"""Where a recorded element can be clicked on its recorded screenshot (training export, arch §4.15).

The browser half of the ``index_to_coordinate`` translator. A BrowserUse or
Skyvern teacher clicks ``[42]``; a vision student has to be taught a point.
This module turns a recorded element into that point, or says why there is
none — off the screenshot, gone from the selector map, covered — and
records how it decided. It reads only what it is handed (a recorded
``DOMState`` and its self-contained selector-map entries, the step's raw DOM
documents) and imports neither ``core.trace`` nor ``spectragent.export``.

**Geometry.** An entry's ``bounds`` are page coordinates of the top-level
document in CSS px, iframe offsets included and independent of the device
scale factor (``dom/models.py``). Minus the document's scroll they are
viewport coordinates; the visible part is that box intersected with the
viewport and with every frame owner's box on the way up (when the element
is found in the recorded tree — ``frame_clip`` says whether it was). An
empty intersection is ``target_offscreen``: a live index click scrolls its
target into view first, which no point on the recorded screenshot can
express. The point is the visible part's centre, in integer CSS px (what a
coordinate action takes). A transformed iframe is treated as a translation.

**Occlusion** (``occlusion="check"``), evidence in this order:

1. the entry's ``hit_test`` (recorded by the ``skyvern_hit_test`` cleaner):
   the point it verified — the centre if that hit, else the grid point that
   did (``visible_point``) — else ``occluded``;
2. the step's ``browser.dom_raw`` document's DOMSnapshot — for an element of
   the main document only (v1): the topmost layout node at the point (the
   largest paint order among those whose box contains it, skipping
   ``pointer-events: none`` and ``visibility: hidden``) must be the element
   or one of its descendants;
3. the recorded tree's ``paint_order`` (BrowserUse cleaning): the same query
   over the surviving nodes of the element's own document — partial cover
   only, since the cleaner already dropped fully covered elements.

When (2) or (3) finds the centre covered, the first uncovered point of a
5x5 grid inset 2 px inside the visible part (row by row) is used instead;
none is ``occluded``. No evidence at all is ``occlusion_unknown`` — the
caller decides whether that raises.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final, Literal

from spectragent.core.capabilities.browser.dom.cleaning.browser_use import (
    REQUIRED_COMPUTED_STYLES,
)
from spectragent.core.capabilities.browser.dom.models import (
    DOMElementNode,
    DOMRect,
    DOMSelectorMap,
    DOMState,
)
from spectragent.schemas.sample import UntranslatableReason

#: Index actions with a coordinate action that does the same thing at a point.
#: Their other params carry over unchanged (``type``'s ``text`` and ``clear``).
COORDINATE_COUNTERPARTS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "browser.click": "browser.click_coordinate",
        "browser.type": "browser.type_coordinate",
    }
)

#: The param an index action addresses its element with.
INDEX_PARAM: Final = "index"
#: The fragment whose selector map the index is a key of.
DOM_FRAGMENT: Final = "browser.dom"
#: The fragment whose ``loader_id`` names the step's document.
META_FRAGMENT: Final = "browser.browser_meta"
#: The archiver whose files hold the step's raw DOM (evidence 2).
DOM_RAW_ARCHIVER: Final = "browser.dom_raw"

Occlusion = Literal["none", "check"]
PointSource = Literal["center", "hit_test", "grid"]
OcclusionVerdict = Literal["clear", "occluded", "unknown", "unchecked"]
Evidence = Literal["hit_test", "dom_raw", "paint_order", "none"]

#: Grid search: points per axis, and the inset from each edge in CSS px —
#: the same grid the ``skyvern_hit_test`` page script walks.
GRID_SIZE: Final = 5
GRID_INSET: Final = 2.0


@dataclass(frozen=True)
class ElementPoint:
    """Where a recorded element can be clicked, or why it cannot.

    Attributes:
        point: Integer CSS px of the viewport, or ``None`` with ``failure``.
        failure: Why there is no point: ``no_counterpart`` (not in the
            recorded selector map), ``target_offscreen``, ``occluded``, or
            ``occlusion_unknown`` (``check`` with no evidence at all).
        detail: A sentence naming the element and the reason.
        provenance: How the point was decided, JSON-safe — the translator's
            ``TranslationRecord.payload``.
    """

    point: tuple[int, int] | None
    failure: UntranslatableReason | None = None
    detail: str = ""
    provenance: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (self.point is None) == (self.failure is None):
            raise ValueError("an ElementPoint carries a point or a failure, never both or neither")


def coordinate_action(
    name: str, params: Mapping[str, Any], point: tuple[int, int]
) -> tuple[str, dict[str, Any]]:
    """The coordinate counterpart of index action *name* at *point*: ``(name, params)``.

    Raises:
        KeyError: *name* has no coordinate counterpart.
    """
    counterpart = COORDINATE_COUNTERPARTS[name]
    rest = {key: value for key, value in params.items() if key != INDEX_PARAM}
    return counterpart, {"coordinate_x": point[0], "coordinate_y": point[1], **rest}


def locate_element(
    element_index: int,
    state: DOMState,
    selector_map: DOMSelectorMap,
    *,
    viewport: tuple[int, int] | None = None,
    occlusion: Occlusion = "check",
    documents: Sequence[Mapping[str, Any]] = (),
    loader_id: str | None = None,
) -> ElementPoint:
    """The point a click on recorded element *element_index* is taught as.

    Args:
        element_index: The selector-map key the teacher's action used.
        state: The step's recorded ``DOMState`` (``DOMObs.element_tree``, its
            tree relinked as ``DOMObs.from_trace`` returns it).
        selector_map: The step's recorded entries (``DOMObs.selector_map``).
        viewport: The screenshot's viewport in CSS px, when known; the point
            is kept inside it as well as inside the DOM's own viewport.
        occlusion: ``check`` consults the evidence; ``none`` does not.
        documents: The step's ``browser.dom_raw`` documents, decoded
            (``archive/dom_raw.read_dom_document``).
        loader_id: The step's document stamp (``browser.browser_meta``) —
            picks the raw document of the same document.
    """
    entry = selector_map.get(element_index)
    if entry is None:
        return ElementPoint(
            None, "no_counterpart", f"element [{element_index}] is not in the recorded selector map"
        )
    provenance: dict[str, Any] = {
        "element_index": element_index,
        "backend_node_id": entry.backend_node_id,
        "target_id": entry.target_id,
    }
    if entry.bounds is None:
        return ElementPoint(
            None,
            "target_offscreen",
            f"element [{element_index}] has no recorded box",
            provenance,
        )
    box = entry.bounds
    provenance["page_bbox"] = [box.x, box.y, box.width, box.height]
    scroll = (state.scroll_info.scroll_x, state.scroll_info.scroll_y)
    node = _find(state.dom_tree, element_index)
    visible = _visible_rect(box, scroll, _viewport(state, viewport), node)
    provenance["frame_clip"] = (
        "unknown" if node is None else "applied" if node.frame_owners() else "none"
    )
    if visible is None:
        return ElementPoint(
            None,
            "target_offscreen",
            f"element [{element_index}] is outside the recorded viewport",
            provenance,
        )
    provenance["visible_rect"] = list(visible)
    center = _inside(visible, ((visible[0] + visible[2]) / 2, (visible[1] + visible[3]) / 2))

    if occlusion == "none":
        return _decided(center, "center", "unchecked", "none", provenance)

    # (1) the in-page hit test, when the cleaner recorded one: the point it
    # verified (its centre, or the grid point that hit), if the screenshot
    # shows it
    hit = entry.hit_test
    if hit is not None:
        if hit.visible_point is not None:
            point = (round(hit.visible_point[0]), round(hit.visible_point[1]))
            if _contains(visible, point):
                source: PointSource = "center" if hit.center_topmost else "hit_test"
                return _decided(point, source, "clear", "hit_test", provenance)
        elif hit.center_topmost:
            return _decided(center, "center", "clear", "hit_test", provenance)
        return _occluded(element_index, "hit_test", provenance)

    # (2) the raw DOM's paint order, (3) the recorded tree's — the first that applies
    candidates: list[tuple[Evidence, Callable[[], _PointQuery | None]]] = [
        ("dom_raw", lambda: _dom_raw_query(entry, node, documents, loader_id)),
        ("paint_order", lambda: _traced_query(node, scroll)),
    ]
    for evidence, build in candidates:
        query = build()
        if query is None:
            continue
        if query(center):
            return _decided(center, "center", "clear", evidence, provenance)
        for point in _grid(visible):
            if query(point):
                return _decided(point, "grid", "clear", evidence, provenance)
        return _occluded(element_index, evidence, provenance)

    provenance.update(point_source=None, occlusion="unknown", evidence="none")
    return ElementPoint(
        None,
        "occlusion_unknown",
        f"element [{element_index}]: the step recorded no occlusion evidence",
        provenance,
    )


# ── geometry ──────────────────────────────────────────────────────────

Rect = tuple[float, float, float, float]  # x1, y1, x2, y2 — half-open


def _viewport(state: DOMState, screenshot: tuple[int, int] | None) -> tuple[float, float]:
    sizes = [
        (state.scroll_info.viewport_width, state.scroll_info.viewport_height),
        *([screenshot] if screenshot is not None else []),
    ]
    widths = [w for w, _ in sizes if w > 0]
    heights = [h for _, h in sizes if h > 0]
    return (min(widths) if widths else 0.0, min(heights) if heights else 0.0)


def _visible_rect(
    box: DOMRect,
    scroll: tuple[float, float],
    viewport: tuple[float, float],
    node: DOMElementNode | None,
) -> Rect | None:
    """The part of *box* the screenshot shows, in viewport CSS px; ``None`` when empty."""
    rect: Rect = (
        box.x - scroll[0],
        box.y - scroll[1],
        box.x - scroll[0] + box.width,
        box.y - scroll[1] + box.height,
    )
    clips: list[Rect] = [(0.0, 0.0, viewport[0], viewport[1])]
    for owner in node.frame_owners() if node is not None else []:
        if owner.bounds is not None:
            b = owner.bounds
            clips.append(
                (
                    b.x - scroll[0],
                    b.y - scroll[1],
                    b.x - scroll[0] + b.width,
                    b.y - scroll[1] + b.height,
                )
            )
    for clip in clips:
        rect = (
            max(rect[0], clip[0]),
            max(rect[1], clip[1]),
            min(rect[2], clip[2]),
            min(rect[3], clip[3]),
        )
    # Visible means at least one whole pixel to click: a sub-pixel sliver has
    # no integer point inside it.
    if math.ceil(rect[0]) >= rect[2] or math.ceil(rect[1]) >= rect[3]:
        return None
    return rect


def _inside(rect: Rect, point: tuple[float, float]) -> tuple[int, int]:
    """*point* rounded to the nearest integer pixel inside *rect* (which has one)."""
    x = min(max(round(point[0]), math.ceil(rect[0])), math.ceil(rect[2]) - 1)
    y = min(max(round(point[1]), math.ceil(rect[1])), math.ceil(rect[3]) - 1)
    return x, y


def _contains(rect: Rect, point: tuple[float, float]) -> bool:
    return rect[0] <= point[0] < rect[2] and rect[1] <= point[1] < rect[3]


def _grid(rect: Rect) -> Iterator[tuple[int, int]]:
    """The 5x5 grid inset 2 px inside *rect*, row by row, as integer pixels."""
    inset_x = min(GRID_INSET, (rect[2] - rect[0]) / 2)
    inset_y = min(GRID_INSET, (rect[3] - rect[1]) / 2)
    step_x = (rect[2] - rect[0] - 2 * inset_x) / (GRID_SIZE - 1)
    step_y = (rect[3] - rect[1] - 2 * inset_y) / (GRID_SIZE - 1)
    for row in range(GRID_SIZE):
        for column in range(GRID_SIZE):
            yield _inside(
                rect, (rect[0] + inset_x + column * step_x, rect[1] + inset_y + row * step_y)
            )


def _find(root: DOMElementNode | None, element_index: int) -> DOMElementNode | None:
    if root is None:
        return None
    if root.element_index == element_index:
        return root
    return next((n for n in root.descendants() if n.element_index == element_index), None)


def _decided(
    point: tuple[int, int],
    source: PointSource,
    verdict: OcclusionVerdict,
    evidence: Evidence,
    provenance: dict[str, Any],
) -> ElementPoint:
    provenance.update(point=list(point), point_source=source, occlusion=verdict, evidence=evidence)
    return ElementPoint(point, None, "", provenance)


def _occluded(element_index: int, evidence: Evidence, provenance: dict[str, Any]) -> ElementPoint:
    provenance.update(point_source=None, occlusion="occluded", evidence=evidence)
    return ElementPoint(
        None,
        "occluded",
        f"element [{element_index}] is covered at every probed point ({evidence})",
        provenance,
    )


# ── occlusion evidence: "is this viewport point the element's?" ─────────

#: Where ``pointer-events`` / ``visibility`` sit in a snapshot's computed
#: style rows — the order every DOMSnapshot capture asks for them in.
_POINTER_EVENTS = REQUIRED_COMPUTED_STYLES.index("pointer-events")
_VISIBILITY = REQUIRED_COMPUTED_STYLES.index("visibility")


#: "Is the element topmost at this viewport point?"
_PointQuery = Callable[[tuple[int, int]], bool]


@dataclass(frozen=True)
class _Painter:
    rect: Rect  # page CSS px
    paint_order: int
    own: bool  # the element itself or one of its descendants


def _topmost_is_own(painters: Sequence[_Painter], page_point: tuple[float, float]) -> bool:
    """Whether the topmost painter at *page_point* belongs to the element.

    Ties go to the element: a node never covers what it shares a paint order
    with. Nothing at all at the point is not the element's either.
    """
    here = [p for p in painters if _contains(p.rect, page_point)]
    if not here:
        return False
    top = max(p.paint_order for p in here)
    return any(p.own for p in here if p.paint_order == top)


def _dom_raw_query(
    entry: DOMElementNode,
    node: DOMElementNode | None,
    documents: Sequence[Mapping[str, Any]],
    loader_id: str | None,
) -> _PointQuery | None:
    """Evidence (2), or ``None`` when it does not apply to this element."""
    if entry.target_id is not None or entry.backend_node_id is None:
        return None  # an out-of-process frame's element: not the main document
    if node is not None and node.frame_owners():
        return None  # v1 judges elements of the main document only
    document = _same_document(documents, loader_id)
    if document is None:
        return None
    capture = document.get("capture") or {}
    snapshots = (capture.get("snapshot") or {}).get("documents") or []
    if not snapshots:
        return None  # a Skyvern JS capture: no snapshot, no paint order
    strings = (capture.get("snapshot") or {}).get("strings") or []
    main = snapshots[0]
    dpr = float(document.get("device_pixel_ratio") or 1.0) or 1.0
    nodes = main.get("nodes") or {}
    layout = main.get("layout") or {}
    backend_ids: list[Any] = nodes.get("backendNodeId") or []
    parents: list[int] = nodes.get("parentIndex") or []
    paint_orders: list[int] = layout.get("paintOrders") or []
    if not paint_orders:
        return None
    try:
        target = backend_ids.index(entry.backend_node_id)
    except ValueError:
        return None
    own = _descendants_of(target, parents)
    painters: list[_Painter] = []
    styles: list[Any] = layout.get("styles") or []
    for li, (ni, bounds) in enumerate(
        zip(layout.get("nodeIndex") or [], layout.get("bounds") or [], strict=False)
    ):
        if li >= len(paint_orders) or len(bounds) < 4 or not _hittable(styles, li, strings):
            continue
        x, y, w, h = (value / dpr for value in bounds[:4])
        painters.append(_Painter((x, y, x + w, y + h), paint_orders[li], ni in own))
    if not any(p.own for p in painters):
        return None  # the element has no layout here: nothing to judge with
    scroll = (main.get("scrollOffsetX", 0.0) / dpr, main.get("scrollOffsetY", 0.0) / dpr)
    return lambda point: _topmost_is_own(painters, (point[0] + scroll[0], point[1] + scroll[1]))


def _same_document(
    documents: Sequence[Mapping[str, Any]], loader_id: str | None
) -> Mapping[str, Any] | None:
    """The raw document of the step's document: its stamp's, else the only one."""
    if loader_id is not None:
        for document in documents:
            if document.get("loader_id") == loader_id:
                return document
    return documents[0] if len(documents) == 1 else None


def _descendants_of(target: int, parents: Sequence[int]) -> set[int]:
    """Snapshot indices of *target* and everything below it."""
    own = {target}
    for index in range(len(parents)):
        chain: list[int] = []
        current = index
        while current >= 0 and current not in own and len(chain) <= len(parents):
            chain.append(current)
            current = parents[current] if current < len(parents) else -1
        if current in own:
            own.update(chain)
    return own


def _hittable(styles: Sequence[Any], li: int, strings: Sequence[str]) -> bool:
    """Whether layout row *li* can receive a click (as far as its styles say)."""
    if li >= len(styles):
        return True
    row = styles[li]

    def value(position: int) -> str | None:
        if position >= len(row):
            return None
        index = row[position]
        return strings[index] if isinstance(index, int) and 0 <= index < len(strings) else None

    return value(_POINTER_EVENTS) != "none" and value(_VISIBILITY) not in ("hidden", "collapse")


def _traced_query(node: DOMElementNode | None, scroll: tuple[float, float]) -> _PointQuery | None:
    """Evidence (3), or ``None`` when the recorded tree has no paint order for the element."""
    if node is None or node.paint_order is None or node.bounds is None:
        return None
    document = _document_owner(node)
    root = node
    while root.parent is not None:
        root = root.parent
    painters = [
        _Painter(
            (n.bounds.x, n.bounds.y, n.bounds.x + n.bounds.width, n.bounds.y + n.bounds.height),
            n.paint_order,
            n is node or _is_within(n, node),
        )
        for n in [root, *root.descendants()]
        if n.paint_order is not None
        and n.bounds is not None
        and _document_owner(n) is document
        and _style_hittable(n)
    ]
    return lambda point: _topmost_is_own(painters, (point[0] + scroll[0], point[1] + scroll[1]))


def _document_owner(node: DOMElementNode) -> DOMElementNode | None:
    owners = node.frame_owners()
    return owners[0] if owners else None


def _is_within(node: DOMElementNode, ancestor: DOMElementNode) -> bool:
    current = node.parent
    while current is not None:
        if current is ancestor:
            return True
        current = current.parent
    return False


def _style_hittable(node: DOMElementNode) -> bool:
    styles = node.computed_styles or {}
    return styles.get("pointer-events") != "none" and styles.get("visibility") not in (
        "hidden",
        "collapse",
    )


__all__ = [
    "COORDINATE_COUNTERPARTS",
    "DOM_FRAGMENT",
    "DOM_RAW_ARCHIVER",
    "INDEX_PARAM",
    "META_FRAGMENT",
    "ElementPoint",
    "coordinate_action",
    "locate_element",
]
