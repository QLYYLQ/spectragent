"""Browser-use 5-stage DOM cleaning pipeline.

Stages:
1. Build simplified tree — filter non-content tags, SVG decorative, hidden
2. Optimize tree — remove empty structural parents
3. Paint-order occlusion — rectangle union, remove fully covered elements
4. Bounds propagation — mark children contained by propagating parents
5. Interactive index assignment — detect interactive, assign indices, build selector_map

Input:  Raw CDP data from 3 protocols (DOM.getDocument, DOMSnapshot, Accessibility)
        + optional JS click listener backend IDs
Output: DOMState (dom_tree + selector_map + scroll_info + serialized_dom)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from spectragent.core.capabilities.browser.dom.cleaning import DOMCleanerBase
from spectragent.core.capabilities.browser.dom.filters.bounds import propagate_parent_bounds
from spectragent.core.capabilities.browser.dom.filters.interactivity import (
    is_dropdown_container,
    is_interactive,
    is_scrollable_container,
)
from spectragent.core.capabilities.browser.dom.filters.occlusion import filter_by_paint_order
from spectragent.core.capabilities.browser.dom.filters.visibility import (
    is_non_content_tag,
    is_svg_decorative,
    is_visible,
)
from spectragent.core.capabilities.browser.dom.models import (
    DOMElementNode,
    DOMRect,
    DOMSelectorMap,
    DOMState,
    EnhancedAXProperty,
    ScrollInfo,
)

logger = logging.getLogger(__name__)

# Computed styles requested in DOMSnapshot.captureSnapshot — order matters
# for index-based parsing of layout['styles'].
# Must match the order in DOMWatchdog._capture_cdp_data().
REQUIRED_COMPUTED_STYLES: list[str] = [
    "display", "visibility", "opacity", "overflow",
    "overflow-x", "overflow-y",
    "cursor", "pointer-events", "position", "background-color",
]

# Overflow values that indicate scrollability.
_SCROLLABLE_OVERFLOW = frozenset({"auto", "scroll", "overlay"})


class BrowserUseCleaner(DOMCleanerBase):
    """5-stage DOM cleaning pipeline, ported from browser-use."""

    __registry_key__ = "browser_use"

    def __init__(
        self,
        *,
        max_occlusion_nodes: int = 5000,
        include_shadow_dom: bool = True,
        enable_compound_components: bool = True,
        device_pixel_ratio: float = 1.0,
    ) -> None:
        self.max_occlusion_nodes = max_occlusion_nodes
        self.include_shadow_dom = include_shadow_dom
        self.enable_compound_components = enable_compound_components
        self.device_pixel_ratio = device_pixel_ratio

    async def clean(
        self,
        raw_dom: dict[str, Any],
        snapshot: dict[str, Any],
        ax_tree: dict[str, Any],
        viewport: tuple[int, int],
        *,
        session_id: str | None = None,
        js_click_listener_ids: set[int] | None = None,
        previous_selector_map: DOMSelectorMap | None = None,
    ) -> DOMState:
        # Phase 0: Parse raw CDP data into DOMElementNode tree.
        root = _build_tree_from_cdp(
            raw_dom, snapshot, ax_tree, session_id,
            device_pixel_ratio=self.device_pixel_ratio,
            js_click_listener_ids=js_click_listener_ids,
        )
        if root is None:
            return DOMState()

        # Stage 1: Build simplified tree — filter non-content.
        _simplify_tree(root)
        logger.debug("Stage 1 (simplify): %d nodes remain", 1 + len(root.descendants()))

        # Stage 2: Optimize — remove empty structural parents.
        _optimize_tree(root)
        logger.debug("Stage 2 (optimize): %d nodes remain", 1 + len(root.descendants()))

        # Stage 3: Paint-order occlusion — mark, don't remove.
        # Matching original browser-use: occluded nodes are flagged
        # (ignored_by_paint_order) so their children survive in the tree
        # and float up in serialization.
        all_nodes = root.descendants()
        occluded = filter_by_paint_order(all_nodes, max_nodes=self.max_occlusion_nodes)
        if occluded:
            _mark_paint_occluded(root, occluded)
            logger.debug("Stage 3 (occlusion): marked %d occluded", len(occluded))

        # Stage 4: Bounds propagation — mark, don't remove.
        # Original uses excluded_by_parent flag: the node's tag is skipped in
        # serialization but children survive (so interactive children like <img>
        # with cursor:pointer still get indexed).
        bounds_excluded = propagate_parent_bounds(root)
        if bounds_excluded:
            _mark_excluded(root, bounds_excluded)
            logger.debug("Stage 4 (bounds): marked %d excluded", len(bounds_excluded))

        # Stage 5: Interactive index assignment.
        selector_map = _assign_interactive_indices(
            root,
            occluded_ids=occluded,
            previous_selector_map=previous_selector_map,
            enable_compound=self.enable_compound_components,
        )
        logger.debug("Stage 5: %d interactive elements", len(selector_map))

        # Extract scroll info from snapshot.
        scroll_info = _extract_scroll_info(snapshot, viewport, self.device_pixel_ratio)

        return DOMState(
            dom_tree=root,
            selector_map=selector_map,
            scroll_info=scroll_info,
        )


# ---------------------------------------------------------------------------
# Phase 0: Parse CDP data → DOMElementNode tree
#
# Strategy (matching original browser-use):
#   1. DOMSnapshot → snapshot_lookup[backend_node_id] for bounds/styles/paint
#   2. Accessibility → ax_map[backend_node_id] for roles/names/properties
#   3. DOM.getDocument → recursive nested tree → DOMElementNode
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class _SnapshotData:
    """Layout data extracted from DOMSnapshot, keyed by backend_node_id."""

    bounds: DOMRect | None = None
    client_rect: DOMRect | None = None
    scroll_rects: DOMRect | None = None
    paint_order: int | None = None
    computed_styles: dict[str, str] | None = None
    cursor_style: str | None = None


def _build_snapshot_lookup(
    snapshot: dict[str, Any],
    device_pixel_ratio: float = 1.0,
) -> dict[int, _SnapshotData]:
    """Build backend_node_id → layout data lookup from DOMSnapshot."""
    documents = snapshot.get("documents", [])
    if not documents:
        return {}

    dpr = device_pixel_ratio if device_pixel_ratio > 0 else 1.0
    strings = snapshot.get("strings", [])
    lookup: dict[int, _SnapshotData] = {}

    for doc in documents:
        nodes = doc.get("nodes", {})
        layout = doc.get("layout", {})

        backend_ids = nodes.get("backendNodeId", [])
        layout_node_indices = layout.get("nodeIndex", [])
        layout_bounds = layout.get("bounds", [])
        layout_paint_orders = layout.get("paintOrders", [])
        layout_styles = layout.get("styles", [])
        layout_client_rects = layout.get("clientRects", [])
        layout_scroll_rects = layout.get("scrollRects", [])

        # snapshot_index → layout_index (first occurrence only).
        layout_map: dict[int, int] = {}
        for li, ni in enumerate(layout_node_indices):
            if ni not in layout_map:
                layout_map[ni] = li

        for snap_idx, bid in enumerate(backend_ids):
            if bid in lookup:
                continue  # First document wins.

            li = layout_map.get(snap_idx)
            if li is None:
                continue  # No layout → not visible.

            bounds: DOMRect | None = None
            if li < len(layout_bounds):
                b = layout_bounds[li]
                if len(b) >= 4:
                    bounds = DOMRect(x=b[0] / dpr, y=b[1] / dpr, width=b[2] / dpr, height=b[3] / dpr)

            paint_order: int | None = None
            if li < len(layout_paint_orders):
                paint_order = layout_paint_orders[li]

            computed_styles: dict[str, str] | None = None
            cursor_style: str | None = None
            if li < len(layout_styles):
                style_indices = layout_styles[li]
                styles: dict[str, str] = {}
                for si, style_idx in enumerate(style_indices):
                    if si < len(REQUIRED_COMPUTED_STYLES) and 0 <= style_idx < len(strings):
                        styles[REQUIRED_COMPUTED_STYLES[si]] = strings[style_idx]
                computed_styles = styles if styles else None
                cursor_style = (styles or {}).get("cursor")

            client_rect: DOMRect | None = None
            if li < len(layout_client_rects):
                cr = layout_client_rects[li]
                if cr and len(cr) >= 4:
                    client_rect = DOMRect(x=cr[0] / dpr, y=cr[1] / dpr, width=cr[2] / dpr, height=cr[3] / dpr)

            scroll_rects: DOMRect | None = None
            if li < len(layout_scroll_rects):
                sr = layout_scroll_rects[li]
                if sr and len(sr) >= 4:
                    scroll_rects = DOMRect(x=sr[0] / dpr, y=sr[1] / dpr, width=sr[2] / dpr, height=sr[3] / dpr)

            lookup[bid] = _SnapshotData(
                bounds=bounds, client_rect=client_rect, scroll_rects=scroll_rects,
                paint_order=paint_order, computed_styles=computed_styles, cursor_style=cursor_style,
            )

    return lookup


def _build_ax_map(
    ax_tree: dict[str, Any],
) -> dict[int, tuple[str, str, list[EnhancedAXProperty]]]:
    """Build backend_node_id → (role, name, properties) from Accessibility tree."""
    ax_map: dict[int, tuple[str, str, list[EnhancedAXProperty]]] = {}
    for ax_node in ax_tree.get("nodes", []):
        bid = ax_node.get("backendDOMNodeId")
        if bid is None:
            continue
        role_val = ax_node.get("role", {}).get("value", "")
        name_val = ax_node.get("name", {}).get("value", "")
        props: list[EnhancedAXProperty] = []
        for prop in ax_node.get("properties", []):
            pname = prop.get("name", "")
            pval_obj = prop.get("value", {})
            pval = pval_obj.get("value") if isinstance(pval_obj, dict) else pval_obj
            if pname:
                props.append(EnhancedAXProperty(name=pname, value=pval))
        ax_map[bid] = (role_val, name_val, props)
    return ax_map


def _is_visible_by_css(
    snap: _SnapshotData | None,
    viewport_threshold: int = 1000,
) -> bool:
    """CSS-based visibility check using snapshot computed styles.

    Checks:
    1. CSS display/visibility/opacity
    2. Must have non-zero bounds
    3. Bounds must not be positioned far off-screen to the left
       (negative x/y beyond threshold — a common "visually hidden" trick)

    NOTE: We do NOT filter by viewport right/bottom edge because DOMSnapshot
    bounds are in **document coordinates** (not viewport-relative).  An element
    at y=3000 may be fully visible after scrolling.  The original browser-use
    accounts for scroll offset per-iframe; we approximate by only filtering
    the clearly off-screen-left/top cases (negative bounds beyond threshold).
    """
    if snap is None or snap.computed_styles is None:
        return False
    cs = snap.computed_styles
    if cs.get("display", "").lower() == "none":
        return False
    if cs.get("visibility", "").lower() == "hidden":
        return False
    try:
        if float(cs.get("opacity", "1")) <= 0:
            return False
    except (ValueError, TypeError):
        pass
    if snap.bounds is None:
        return False
    b = snap.bounds
    # NOTE: No zero-area check — matching original browser-use behavior.
    # Zero-area elements (width=0, height=0) can still be visible/interactive
    # (e.g. <source> elements inheriting cursor:pointer from parent <a>).
    # Off-screen-left/top check: elements with large negative x/y are
    # visually hidden (e.g. position:fixed; left:-9999px).
    if b.x + b.width < -viewport_threshold:
        return False
    if b.y + b.height < -viewport_threshold:
        return False
    return True


def _detect_scrollable(snap: _SnapshotData | None) -> bool:
    """Detect scrollability from overflow CSS + scroll/client rects."""
    if snap is None or snap.computed_styles is None or snap.scroll_rects is None or snap.client_rect is None:
        return False
    cs = snap.computed_styles
    overflow = cs.get("overflow", "visible").lower()
    overflow_x = cs.get("overflow-x", overflow).lower()
    overflow_y = cs.get("overflow-y", overflow).lower()
    if not any(v in _SCROLLABLE_OVERFLOW for v in (overflow, overflow_x, overflow_y)):
        return False
    return (
        snap.scroll_rects.height > (snap.client_rect.height + 1)
        or snap.scroll_rects.width > (snap.client_rect.width + 1)
    )


def _is_visible_in_all_frames(
    snap: _SnapshotData | None,
    html_frames: list[tuple[str, _SnapshotData]],
    viewport_threshold: int = 1000,
) -> bool:
    """Full visibility check matching original ``is_element_visible_according_to_all_parents``.

    Checks CSS properties, then verifies the element's bounds intersect
    with each parent HTML/IFRAME frame's viewport (accounting for scroll).
    """
    if not _is_visible_by_css(snap):
        return False

    # At this point snap and snap.bounds are guaranteed non-None by _is_visible_by_css.
    assert snap is not None and snap.bounds is not None
    bx, by = snap.bounds.x, snap.bounds.y
    bw, bh = snap.bounds.width, snap.bounds.height

    # Reverse-iterate through parent frames, matching original logic.
    for frame_tag, frame_snap in reversed(html_frames):
        # IFRAME/FRAME: translate bounds by iframe position.
        if frame_tag.upper() in ("IFRAME", "FRAME") and frame_snap.bounds:
            bx += frame_snap.bounds.x
            by += frame_snap.bounds.y

        # HTML frame: check viewport intersection with scroll offset.
        if (
            frame_tag == "HTML"
            and frame_snap.scroll_rects
            and frame_snap.client_rect
        ):
            vp_w = frame_snap.client_rect.width
            vp_h = frame_snap.client_rect.height
            adj_x = bx - frame_snap.scroll_rects.x
            adj_y = by - frame_snap.scroll_rects.y

            if not (
                adj_x < vp_w
                and adj_x + bw > 0
                and adj_y < vp_h + viewport_threshold
                and adj_y + bh > -viewport_threshold
            ):
                return False

            # Adjust for next frame in the chain.
            bx -= frame_snap.scroll_rects.x
            by -= frame_snap.scroll_rects.y

    return True


def _construct_node(
    node: dict[str, Any],
    snapshot_lookup: dict[int, _SnapshotData],
    ax_map: dict[int, tuple[str, str, list[EnhancedAXProperty]]],
    js_listener_ids: set[int],
    session_id: str | None,
    node_memo: dict[int, DOMElementNode],
    html_frames: list[tuple[str, _SnapshotData]] | None = None,
) -> DOMElementNode | None:
    """Recursively build DOMElementNode from DOM.getDocument node dict.

    Mirrors original browser-use ``_construct_enhanced_node`` (service.py:695-876).
    Tracks ``html_frames`` for per-frame viewport visibility filtering.
    """
    if html_frames is None:
        html_frames = []

    ntype = node.get("nodeType", 1)

    # DOCUMENT_NODE (9): skip wrapper, recurse children, return first result.
    if ntype == 9:
        for child in node.get("children", []):
            result = _construct_node(child, snapshot_lookup, ax_map, js_listener_ids, session_id, node_memo, html_frames)
            if result is not None:
                return result
        return None

    # Only process ELEMENT (1), TEXT (3), DOCUMENT_FRAGMENT (11).
    if ntype not in (1, 3, 11):
        return None

    nid = node.get("nodeId", 0)
    if nid in node_memo:
        return node_memo[nid]

    bid: int | None = node.get("backendNodeId")
    snap = snapshot_lookup.get(bid) if bid else None

    # TEXT_NODE (3).
    if ntype == 3:
        text_val = node.get("nodeValue", "") or ""
        # Text visibility: must have snapshot layout data + non-trivial text
        # + bounds not far off-screen. Matches original: `snapshot_node and is_visible`.
        text_vis = False
        if (
            snap is not None
            and snap.bounds is not None
            and text_val.strip()
            and len(text_val.strip()) > 1
        ):
            b = snap.bounds
            # Same off-screen check as _is_visible_by_css.
            if not (b.x + b.width < -1000 or b.y + b.height < -1000):
                text_vis = True
        text_node = DOMElementNode(
            tag_name="#text",
            node_type=3,
            node_value=text_val,
            node_id=nid,
            backend_node_id=bid,
            session_id=session_id,
            bounds=snap.bounds if snap else None,
            is_visible=text_vis,
        )
        node_memo[nid] = text_node
        return text_node

    # ELEMENT_NODE (1) or DOCUMENT_FRAGMENT_NODE (11).
    tag = node.get("nodeName", "").lower()

    # Attributes (flat pairs in DOM.getDocument).
    attrs: dict[str, str] = {}
    raw_attrs = node.get("attributes", [])
    if raw_attrs:
        for j in range(0, len(raw_attrs) - 1, 2):
            attrs[raw_attrs[j]] = raw_attrs[j + 1]

    # Shadow root type.
    shadow_root_type: str | None = node.get("shadowRootType") or None

    # Visibility: full per-frame check matching original.
    is_vis = _is_visible_in_all_frames(snap, html_frames)
    is_scroll = _detect_scrollable(snap)

    # File input override: always visible.
    if not is_vis and tag == "input" and attrs.get("type") == "file":
        is_vis = True
    # Shadow host override: always visible.
    if not is_vis and shadow_root_type:
        is_vis = True

    # AX data.
    ax_role: str | None = None
    ax_name: str | None = None
    ax_properties: list[EnhancedAXProperty] | None = None
    if bid and bid in ax_map:
        role_str, name_str, props_list = ax_map[bid]
        ax_role = role_str or None
        ax_name = name_str or None
        ax_properties = props_list if props_list else None

    dom_node = DOMElementNode(
        tag_name=tag,
        node_type=ntype,
        node_id=nid,
        backend_node_id=bid,
        session_id=session_id,
        attributes=attrs,
        is_visible=is_vis,
        is_scrollable=is_scroll,
        bounds=snap.bounds if snap else None,
        client_rect=snap.client_rect if snap else None,
        scroll_rects=snap.scroll_rects if snap else None,
        computed_styles=snap.computed_styles if snap else None,
        cursor_style=snap.cursor_style if snap else None,
        paint_order=snap.paint_order if snap else None,
        ax_role=ax_role,
        ax_name=ax_name,
        ax_properties=ax_properties,
        shadow_root_type=shadow_root_type,
        has_js_click_listener=(bid in js_listener_ids) if bid else False,
    )
    node_memo[nid] = dom_node

    # --- Track html_frames for children (matching original lines 814-838) ---
    updated_frames = html_frames.copy()
    node_name_upper = node.get("nodeName", "").upper()

    # HTML frame with frameId → add to frame list.
    if ntype == 1 and node_name_upper == "HTML" and node.get("frameId") is not None:
        if snap:
            updated_frames.append(("HTML", snap))

    # IFRAME/FRAME with bounds → add to frame list.
    if node_name_upper in ("IFRAME", "FRAME") and snap and snap.bounds:
        updated_frames.append((node_name_upper, snap))

    # --- Recurse children with updated frame tracking ---

    # Shadow roots.
    if node.get("shadowRoots"):
        dom_node.shadow_roots = []
        for sr in node["shadowRoots"]:
            sr_node = _construct_node(sr, snapshot_lookup, ax_map, js_listener_ids, session_id, node_memo, updated_frames)
            if sr_node is not None:
                sr_node.parent = dom_node
                dom_node.shadow_roots.append(sr_node)

    # Content document (iframes).
    if node.get("contentDocument"):
        cd = _construct_node(node["contentDocument"], snapshot_lookup, ax_map, js_listener_ids, session_id, node_memo, updated_frames)
        if cd is not None:
            cd.parent = dom_node
            dom_node.content_document = cd

    # Children — skip shadow root nodeIds to avoid duplication.
    shadow_nids: set[int] = set()
    if node.get("shadowRoots"):
        for sr in node["shadowRoots"]:
            shadow_nids.add(sr.get("nodeId", -1))

    for child in node.get("children", []):
        if child.get("nodeId", -1) in shadow_nids:
            continue
        child_node = _construct_node(child, snapshot_lookup, ax_map, js_listener_ids, session_id, node_memo, updated_frames)
        if child_node is not None:
            child_node.parent = dom_node
            dom_node.children.append(child_node)

    return dom_node


def _build_tree_from_cdp(
    raw_dom: dict[str, Any],
    snapshot: dict[str, Any],
    ax_tree: dict[str, Any],
    session_id: str | None,
    *,
    device_pixel_ratio: float = 1.0,
    js_click_listener_ids: set[int] | None = None,
) -> DOMElementNode | None:
    """Merge 3 CDP protocol results into a unified DOMElementNode tree.

    Strategy (matching original browser-use):
    1. DOMSnapshot → snapshot_lookup[backend_node_id] for bounds/styles
    2. Accessibility → ax_map[backend_node_id] for ARIA data
    3. DOM.getDocument → recursive tree → DOMElementNode
    """
    root_data = raw_dom.get("root")
    if root_data is None:
        return None

    snapshot_lookup = _build_snapshot_lookup(snapshot, device_pixel_ratio)
    ax_map = _build_ax_map(ax_tree)
    node_memo: dict[int, DOMElementNode] = {}

    return _construct_node(
        root_data, snapshot_lookup, ax_map,
        js_listener_ids=js_click_listener_ids or set(),
        session_id=session_id, node_memo=node_memo,
    )


# ---------------------------------------------------------------------------
# JS click listener flag application
# ---------------------------------------------------------------------------

def _apply_js_listener_flags(root: DOMElementNode, ids: set[int]) -> None:
    """Set ``has_js_click_listener`` on nodes whose backend_node_id is in *ids*."""
    stack = [root]
    while stack:
        node = stack.pop()
        if node.backend_node_id is not None and node.backend_node_id in ids:
            node.has_js_click_listener = True
        stack.extend(node.children)


# ---------------------------------------------------------------------------
# Stage 1: Simplify tree
# ---------------------------------------------------------------------------

def _simplify_tree(node: DOMElementNode) -> None:
    """Remove non-content elements in-place (depth-first)."""
    node.children = [c for c in node.children if _should_keep(c)]
    for child in node.children:
        _simplify_tree(child)


def _should_keep(node: DOMElementNode) -> bool:
    tag = node.tag_name.lower()
    if is_non_content_tag(tag):
        return False
    # SVG decorative children.
    parent_tag = node.parent.tag_name.lower() if node.parent else ""
    if parent_tag == "svg" and is_svg_decorative(tag):
        return False
    # Keep if visible, scrollable, has children, or is shadow host.
    if is_visible(node):
        return True
    if node.is_scrollable:
        return True
    if node.children:
        return True
    if node.shadow_roots:
        return True
    if node.shadow_root_type is not None:
        return True
    # Validation attribute override: keep in tree for aria-*/pseudo attrs
    # even if not CSS-visible (matches original browser-use behavior).
    if node.attributes and any(k.startswith(("aria-", "pseudo")) for k in node.attributes):
        return True
    return False


# ---------------------------------------------------------------------------
# Stage 2: Optimize tree
# ---------------------------------------------------------------------------

def _optimize_tree(node: DOMElementNode) -> None:
    """Remove structural-only nodes with no visible content."""
    for child in node.children:
        _optimize_tree(child)
    node.children = [c for c in node.children if _keep_in_optimized(c)]


def _keep_in_optimized(node: DOMElementNode) -> bool:
    if node.node_type == 3:  # TEXT_NODE
        return bool(node.node_value and node.node_value.strip())
    if is_visible(node):
        return True
    if node.is_scrollable:
        return True
    if node.children:
        return True
    if node.shadow_roots:
        return True
    return False


# ---------------------------------------------------------------------------
# Stage 5: Interactive index assignment
# ---------------------------------------------------------------------------

def _assign_interactive_indices(
    root: DOMElementNode,
    *,
    occluded_ids: set[int] | None = None,
    previous_selector_map: DOMSelectorMap | None = None,
    enable_compound: bool = True,
) -> DOMSelectorMap:
    """Walk tree, detect interactive elements, assign indices, build selector_map.

    selector_map is keyed by ``backend_node_id`` (matching browser-use behavior).
    ``element_index`` is still assigned as a sequential counter for debugging.
    """
    _occluded = occluded_ids or set()
    selector_map: DOMSelectorMap = {}
    index_counter = [0]  # mutable counter

    def _walk(node: DOMElementNode) -> None:
        should_index = False

        # Nodes excluded by bounds propagation don't get indexed
        # (matching original excluded_by_parent behavior) but their
        # children are still walked.
        # Nodes occluded by paint order (ignored_by_paint_order in original)
        # also don't get indexed.
        is_occluded = node.backend_node_id is not None and node.backend_node_id in _occluded
        if node.should_display and not is_occluded:
            # Scrollable containers are indexed even if not in viewport
            # (agent can scroll the page to reach them).
            if is_scrollable_container(node):
                should_index = True
            # Everything else requires visibility.
            elif is_visible(node):
                if is_interactive(node) or is_dropdown_container(node):
                    node.is_interactive = True
                    should_index = True

        if should_index and node.backend_node_id is not None:
            node.element_index = index_counter[0]
            selector_map[node.backend_node_id] = node
            index_counter[0] += 1

            # New node tracking.
            if previous_selector_map is not None:
                if node.backend_node_id not in previous_selector_map:
                    node.is_new = True

            # Compound components.
            if enable_compound:
                _add_compound_components(node)

        for child in node.children:
            _walk(child)

    _walk(root)
    return selector_map


# ---------------------------------------------------------------------------
# Compound components
# ---------------------------------------------------------------------------

def _add_compound_components(node: DOMElementNode) -> None:
    """Enhance compound controls with virtual sub-component info.

    Ported from browser-use DOMTreeSerializer._add_compound_components().
    """
    node._compound_children.clear()  # Idempotent — safe if called twice.
    tag = node.tag_name.lower()
    attrs = node.attributes

    if tag == "input":
        input_type = attrs.get("type", "").lower()
        if input_type == "range":
            min_val = _safe_parse_number(attrs.get("min", "0"), 0.0)
            max_val = _safe_parse_number(attrs.get("max", "100"), 100.0)
            node._compound_children.append({
                "role": "slider", "name": "Value",
                "valuemin": min_val, "valuemax": max_val, "valuenow": None,
            })
        elif input_type == "number":
            min_val = _safe_parse_optional_number(attrs.get("min"))
            max_val = _safe_parse_optional_number(attrs.get("max"))
            node._compound_children.extend([
                {"role": "button", "name": "Increment", "valuemin": None, "valuemax": None, "valuenow": None},
                {"role": "button", "name": "Decrement", "valuemin": None, "valuemax": None, "valuenow": None},
                {"role": "textbox", "name": "Value", "valuemin": min_val, "valuemax": max_val, "valuenow": None},
            ])
        elif input_type == "color":
            node._compound_children.extend([
                {"role": "textbox", "name": "Hex Value", "valuemin": None, "valuemax": None, "valuenow": None},
                {"role": "button", "name": "Color Picker", "valuemin": None, "valuemax": None, "valuenow": None},
            ])
        elif input_type == "file":
            multiple = "multiple" in attrs
            # Extract current file selection state from AX properties.
            current_value = "None"
            if node.ax_properties:
                for prop in node.ax_properties:
                    if prop.name == "valuetext" and prop.value:
                        val = str(prop.value).strip()
                        if val and val.lower() not in ("", "no file chosen", "no file selected"):
                            current_value = val
                        break
                    elif prop.name == "value" and prop.value:
                        val = str(prop.value).strip()
                        if val:
                            # Extract filename from path.
                            for sep in ("\\", "/"):
                                if sep in val:
                                    val = val.split(sep)[-1]
                            current_value = val
                        break
            node._compound_children.extend([
                {"role": "button", "name": "Browse Files", "valuemin": None, "valuemax": None, "valuenow": None},
                {
                    "role": "textbox",
                    "name": f"{'Files' if multiple else 'File'} Selected",
                    "valuemin": None, "valuemax": None, "valuenow": current_value,
                },
            ])
        # date/time/datetime-local/month/week — skip compound (format in placeholder).

    elif tag == "select":
        base = [
            {"role": "button", "name": "Dropdown Toggle", "valuemin": None, "valuemax": None, "valuenow": None},
        ]
        options_info = _extract_select_options(node)
        if options_info:
            opt_comp: dict[str, Any] = {
                "role": "listbox", "name": "Options",
                "valuemin": None, "valuemax": None, "valuenow": None,
                "options_count": options_info["count"],
                "first_options": options_info["first_options"],
            }
            if options_info.get("format_hint"):
                opt_comp["format_hint"] = options_info["format_hint"]
            base.append(opt_comp)
        else:
            base.append({"role": "listbox", "name": "Options", "valuemin": None, "valuemax": None, "valuenow": None})
        node._compound_children.extend(base)

    elif tag == "details":
        node._compound_children.extend([
            {"role": "button", "name": "Toggle Disclosure", "valuemin": None, "valuemax": None, "valuenow": None},
            {"role": "region", "name": "Content Area", "valuemin": None, "valuemax": None, "valuenow": None},
        ])

    elif tag == "audio":
        node._compound_children.extend([
            {"role": "button", "name": "Play/Pause", "valuemin": None, "valuemax": None, "valuenow": None},
            {"role": "slider", "name": "Progress", "valuemin": 0, "valuemax": 100, "valuenow": None},
            {"role": "button", "name": "Mute", "valuemin": None, "valuemax": None, "valuenow": None},
            {"role": "slider", "name": "Volume", "valuemin": 0, "valuemax": 100, "valuenow": None},
        ])

    elif tag == "video":
        node._compound_children.extend([
            {"role": "button", "name": "Play/Pause", "valuemin": None, "valuemax": None, "valuenow": None},
            {"role": "slider", "name": "Progress", "valuemin": 0, "valuemax": 100, "valuenow": None},
            {"role": "button", "name": "Mute", "valuemin": None, "valuemax": None, "valuenow": None},
            {"role": "slider", "name": "Volume", "valuemin": 0, "valuemax": 100, "valuenow": None},
            {"role": "button", "name": "Fullscreen", "valuemin": None, "valuemax": None, "valuenow": None},
        ])


def _extract_select_options(select_node: DOMElementNode) -> dict[str, Any] | None:
    """Extract option information from a <select> element."""
    if not select_node.children:
        return None

    options: list[dict[str, str]] = []
    option_values: list[str] = []

    def _recurse(node: DOMElementNode) -> None:
        tag = node.tag_name.lower()
        if tag == "option":
            text = node.text_content.strip() if node.text_content else ""
            value = node.attributes.get("value", "").strip()
            if not value and text:
                value = text
            if text or value:
                options.append({"text": text, "value": value})
                option_values.append(value)
        else:
            for child in node.children:
                _recurse(child)

    for child in select_node.children:
        _recurse(child)

    if not options:
        return None

    first_options: list[str] = []
    for opt in options[:4]:
        display = opt["text"] or opt["value"]
        if display:
            first_options.append(display[:30] + ("..." if len(display) > 30 else ""))

    if len(options) > 4:
        first_options.append(f"... {len(options) - 4} more options...")

    # Infer format hint.
    format_hint: str | None = None
    sample = [v for v in option_values[:5] if v]
    if len(sample) >= 2:
        if all(v.isdigit() for v in sample):
            format_hint = "numeric"
        elif all(len(v) == 2 and v.isupper() for v in sample):
            format_hint = "country/state codes"
        elif all("/" in v or "-" in v for v in sample):
            format_hint = "date/path format"
        elif any("@" in v for v in sample):
            format_hint = "email addresses"

    return {"count": len(options), "first_options": first_options, "format_hint": format_hint}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _remove_nodes_by_id(root: DOMElementNode, ids: set[int]) -> None:
    """Remove nodes whose backend_node_id is in *ids* (in-place)."""
    root.children = [c for c in root.children if c.backend_node_id not in ids]
    for child in root.children:
        _remove_nodes_by_id(child, ids)


def _mark_paint_occluded(root: DOMElementNode, ids: set[int]) -> None:
    """Mark paint-occluded nodes as should_display=False.

    Matching original browser-use behavior: occluded nodes keep their
    children in the tree (children float up in serialization) but the
    node's own tag is skipped and it doesn't get an interactive index.
    """
    stack = [root]
    while stack:
        node = stack.pop()
        if node.backend_node_id is not None and node.backend_node_id in ids:
            node.should_display = False
        stack.extend(node.children)


def _mark_excluded(root: DOMElementNode, ids: set[int]) -> None:
    """Mark nodes as should_display=False (keep in tree for interactivity detection)."""
    stack = [root]
    while stack:
        node = stack.pop()
        if node.backend_node_id is not None and node.backend_node_id in ids:
            node.should_display = False
        stack.extend(node.children)


def _extract_scroll_info(
    snapshot: dict[str, Any],
    viewport: tuple[int, int],
    device_pixel_ratio: float = 1.0,
) -> ScrollInfo:
    """Extract scroll info from snapshot metadata."""
    documents = snapshot.get("documents", [])
    if not documents:
        return ScrollInfo(viewport_width=viewport[0], viewport_height=viewport[1])
    doc = documents[0]
    dpr = device_pixel_ratio if device_pixel_ratio > 0 else 1.0
    scroll_x = doc.get("scrollOffsetX", 0.0) / dpr
    scroll_y = doc.get("scrollOffsetY", 0.0) / dpr
    # contentWidth/contentHeight from snapshot layout.
    layout = doc.get("layout", {})
    bounds = layout.get("bounds", [])
    max_x = max_y = 0.0
    for b in bounds:
        if len(b) >= 4:
            max_x = max(max_x, (b[0] + b[2]) / dpr)
            max_y = max(max_y, (b[1] + b[3]) / dpr)

    return ScrollInfo(
        scroll_x=scroll_x,
        scroll_y=scroll_y,
        scroll_width=max_x,
        scroll_height=max_y,
        viewport_width=viewport[0],
        viewport_height=viewport[1],
    )


def _safe_parse_number(val: str, default: float) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _safe_parse_optional_number(val: str | None) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
