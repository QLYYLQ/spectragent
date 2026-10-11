"""A recorded sticky-header page, as the DOM fragment and the raw DOM would hold it (M32).

The geometry of the fixture site's ``sticky`` page (``fixture_site.py``),
written once and compiled into the two shapes the occlusion evidence reads:
a ``DOMState`` with self-contained selector-map entries (what ``browser.dom``
records — with ``paint_order`` like BrowserUse cleaning, or ``hit_test`` like
``skyvern_hit_test``), and a ``browser.dom_raw`` document whose DOMSnapshot
carries device-pixel bounds and paint orders.

Page coordinates, CSS px, scrolled ``SCROLL_Y`` down in a 1280x720 viewport:
``nav`` (sticky, 100 tall, painted over the links) is at page y 500..600 —
viewport 0..100. ``covered`` lies wholly under it; ``partial`` has its centre
under it and its lower part visible; ``clear`` is uncovered; ``below`` is
past the viewport; ``frame`` (a same-process iframe) holds ``inside``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from spectragent.core.capabilities.browser.dom.cleaning.browser_use import (
    REQUIRED_COMPUTED_STYLES,
)
from spectragent.core.capabilities.browser.dom.models import (
    DOMElementNode,
    DOMRect,
    DOMState,
    HitTest,
    ScrollInfo,
)

SCROLL_Y = 500.0
VIEWPORT = (1280, 720)
LOADER_ID = "loader-sticky"


@dataclass(frozen=True)
class Box:
    """One element of the page: page box (x, y, w, h), paint order, parent."""

    name: str
    backend_id: int
    box: tuple[float, float, float, float]
    paint_order: int
    parent: str | None
    indexed: bool = True
    tag: str = "a"


#: The main document, in snapshot order.
MAIN: tuple[Box, ...] = (
    Box("html", 1, (0, 0, 1280, 2400), 0, None, indexed=False, tag="html"),
    Box("body", 2, (0, 0, 1280, 2400), 1, "html", indexed=False, tag="body"),
    Box("nav", 3, (0, 500, 1280, 100), 10, "body", indexed=False, tag="header"),
    Box("home", 4, (0, 500, 45, 20), 11, "nav"),
    Box("covered", 5, (20, 530, 240, 40), 2, "body"),
    Box("partial", 6, (300, 540, 240, 100), 3, "body"),
    Box("clear", 7, (580, 700, 240, 40), 4, "body"),
    Box("below", 9, (20, 1800, 240, 40), 5, "body"),
    Box("frame", 10, (860, 600, 300, 120), 6, "body", tag="iframe"),
)
#: The iframe's document (its own paint-order space).
FRAME: tuple[Box, ...] = (
    Box("frame_html", 20, (860, 600, 300, 120), 0, None, indexed=False, tag="html"),
    Box("inside", 21, (868, 608, 48, 17), 1, "frame_html", tag="button"),
)

INDEX: Mapping[str, int] = {box.name: box.backend_id for box in (*MAIN, *FRAME) if box.indexed}


def sticky_state(
    *,
    paint_orders: bool = True,
    hit_tests: Mapping[str, HitTest] | None = None,
    extra: tuple[Box, ...] = (),
) -> DOMState:
    """The recorded ``DOMState``: BrowserUse-like (``paint_orders``) or Skyvern-like."""
    nodes: dict[str, DOMElementNode] = {}
    for box in (*MAIN, *extra, *FRAME):
        x, y, w, h = box.box
        node = DOMElementNode(
            tag_name=box.tag,
            attributes={"id": box.name},
            backend_node_id=box.backend_id,
            bounds=DOMRect(x, y, w, h),
            paint_order=box.paint_order if paint_orders else None,
            element_index=box.backend_id if box.indexed else None,
            is_interactive=box.indexed,
            hit_test=(hit_tests or {}).get(box.name),
        )
        nodes[box.name] = node
        if box.parent is not None:
            node.parent = nodes[box.parent]
            nodes[box.parent].children.append(node)
    frame_root = nodes["frame_html"]
    frame_root.parent = nodes["frame"]
    nodes["frame"].content_document = frame_root
    return DOMState(
        dom_tree=nodes["html"],
        selector_map={node.element_index: node for node in nodes.values() if node.element_index},
        scroll_info=ScrollInfo(
            scroll_y=SCROLL_Y,
            scroll_width=1280,
            scroll_height=2400,
            viewport_width=VIEWPORT[0],
            viewport_height=VIEWPORT[1],
        ),
    )


def overlay(paint_order: int = 99) -> Box:
    """A full-viewport box painted over everything (``pointer-events: none`` in the snapshot)."""
    return Box(
        "overlay", 30, (0, SCROLL_Y, 1280, 720), paint_order, "body", indexed=False, tag="div"
    )


def raw_document(
    *,
    dpr: float = 1.0,
    loader_id: str | None = LOADER_ID,
    extra: tuple[Box, ...] = (),
    click_through: tuple[str, ...] = (),
) -> dict[str, Any]:
    """A ``browser.dom_raw`` document of the main document: DOMSnapshot bounds in device px.

    *click_through* names boxes whose computed ``pointer-events`` is ``none``.
    """
    boxes = (*MAIN, *extra)
    position = {box.name: i for i, box in enumerate(boxes)}
    strings = ["auto", "none"]
    pointer_events = REQUIRED_COMPUTED_STYLES.index("pointer-events")
    styles = []
    for box in boxes:
        row = [0] * len(REQUIRED_COMPUTED_STYLES)
        row[pointer_events] = 1 if box.name in click_through else 0
        styles.append(row)
    return {
        "capture": {
            "raw_dom": {},
            "snapshot": {
                "strings": strings,
                "documents": [
                    {
                        "nodes": {
                            "backendNodeId": [box.backend_id for box in boxes],
                            "parentIndex": [
                                position[box.parent] if box.parent else -1 for box in boxes
                            ],
                        },
                        "layout": {
                            "nodeIndex": list(range(len(boxes))),
                            "bounds": [[value * dpr for value in box.box] for box in boxes],
                            "paintOrders": [box.paint_order for box in boxes],
                            "styles": styles,
                        },
                        "scrollOffsetX": 0.0,
                        "scrollOffsetY": SCROLL_Y * dpr,
                    }
                ],
            },
            "ax_tree": {},
        },
        "device_pixel_ratio": dpr,
        "viewport": list(VIEWPORT),
        "cleaner": "browser_use",
        "source": "cycle",
        "tab": "tab-1",
        "loader_id": loader_id,
        "url": "http://shop.example.test/sticky/page.html",
    }
