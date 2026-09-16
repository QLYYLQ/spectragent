"""Browser-use 5-stage DOM cleaning pipeline.

Stages:
1. Build simplified tree — filter non-content tags, SVG decorative, hidden
2. Optimize tree — remove empty structural parents
3. Paint-order occlusion — rectangle union, remove fully covered elements
4. Bounds propagation — mark children contained by propagating parents
5. Interactive index assignment — detect interactive, assign indices, build selector_map

Input:  ``TargetCapture`` — raw CDP data from 3 protocols (DOM.getDocument,
        DOMSnapshot, Accessibility) + optional JS click listener backend IDs,
        for the tab's target and, nested, each out-of-process frame target
Output: DOMState (dom_tree + selector_map + scroll_info + serialized_dom)

Every stage walks the *composed* tree — light DOM, shadow roots and frame
documents, the same order ``TextSerializer`` prints — so whatever reaches the
prompt can also reach the selector map. Bounds are converted to page
coordinates while the tree is built (see ``DOMElementNode.bounds``), so a
frame's content is filtered and indexed against the page it is shown in.

A frame's document is built only for a frame that is rendered: an owner that
is not visible (``display:none``, ``visibility:hidden``, ``opacity:0``, no
layout, or outside the viewports around it) contributes no document — CSS
visibility does not inherit across the frame boundary, but painting and
hit-testing stop there all the same.

One deliberate divergence from upstream browser-use
-----------------------------------------------------

``descendants_occlude`` defaults to ``False`` here and upstream has no such
switch: upstream always counts a node's own descendants among the opaque
boxes that can hide it. A click on a descendant *is* a click on the node —
the event reaches it — so an element covered only by its own subtree is not
hidden from anybody, and counting it drops exactly the controls that paint
their face with an inner element. reCAPTCHA's ``span#recaptcha-anchor``
(role=checkbox, under its own white ``div.recaptcha-checkbox-border``) is
the case that motivated it: with parity it is never indexed, so the widget
cannot be clicked by index at all. Set ``descendants_occlude: true`` in the
capability's ``dom_cleaner`` block for upstream parity; both behaviours are
pinned in ``tests/test_core/test_capabilities/test_browser_dom_occlusion.py``.

Every knob here arrives on the *instance* the capability was configured
with (``BrowserCapability.__wiring__["dom_cleaner"]``), so the generated
``BrowserUseDom_CleanerConfig`` is the config surface for all of them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any

from spectragent.core.capabilities.browser.dom.cleaning import DOMCleanerBase
from spectragent.core.capabilities.browser.dom.filters.bounds import propagate_parent_bounds
from spectragent.core.capabilities.browser.dom.filters.interactivity import (
    is_dropdown_container,
    is_interactive,
    is_scrollable_container,
)
from spectragent.core.capabilities.browser.dom.filters.occlusion import (
    filter_by_paint_order,
    has_paint_geometry,
)
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
    TargetCapture,
    element_handle,
)

logger = logging.getLogger(__name__)

# Computed styles requested in DOMSnapshot.captureSnapshot — order matters
# for index-based parsing of layout['styles']. ``DOMWatchdog`` passes this
# list to the capture, so the request and the parse cannot drift apart.
REQUIRED_COMPUTED_STYLES: list[str] = [
    "display", "visibility", "opacity", "overflow",
    "overflow-x", "overflow-y",
    "cursor", "pointer-events", "position", "background-color",
    # A frame owner's border and padding: its document starts inside both.
    "border-left-width", "border-top-width", "padding-left", "padding-top",
]

# Overflow values that indicate scrollability.
_SCROLLABLE_OVERFLOW = frozenset({"auto", "scroll", "overlay"})


class BrowserUseCleaner(DOMCleanerBase):
    """5-stage DOM cleaning pipeline, ported from browser-use.

    ``max_occlusion_nodes``: stage 3 is skipped when the whole composed tree
    (frame documents and shadow roots included) has more nodes with paint
    geometry than this.

    ``include_shadow_dom``: build shadow-root content into the tree. Off,
    shadow roots are left out of the tree entirely — never printed, never
    indexed.

    ``descendants_occlude``: whether stage 3 counts a node's own descendants
    among the boxes that can cover it. Upstream browser-use does (``True``).
    The default does not, because a click on a descendant *is* a click on
    the node — the event reaches it — so a control that paints its face with
    an opaque inner element (reCAPTCHA's ``span#recaptcha-anchor`` under its
    own ``div.recaptcha-checkbox-border``) stays a target. See the module
    docstring for the full rationale and
    ``filters/occlusion.filter_by_paint_order`` for the algorithm.

    All four are reachable from YAML: this class is the ``dom_cleaner``
    receptor of ``BrowserCapability``, so the Composer builds *this
    instance* and ``DOMWatchdog`` cleans with it.
    """

    __registry_key__ = "browser_use"

    def __init__(
        self,
        *,
        max_occlusion_nodes: int = 5000,
        include_shadow_dom: bool = True,
        enable_compound_components: bool = True,
        descendants_occlude: bool = False,
    ) -> None:
        self.max_occlusion_nodes = max_occlusion_nodes
        self.include_shadow_dom = include_shadow_dom
        self.enable_compound_components = enable_compound_components
        self.descendants_occlude = descendants_occlude

    async def clean(
        self,
        capture: TargetCapture,
        viewport: tuple[int, int],
        *,
        previous_selector_map: DOMSelectorMap | None = None,
        device_pixel_ratio: float = 1.0,
    ) -> DOMState:
        # Phase 0: Parse raw CDP data into DOMElementNode tree.
        root = _build_tree_from_cdp(
            capture,
            device_pixel_ratio=device_pixel_ratio,
            include_shadow_dom=self.include_shadow_dom,
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
        occluded = _mark_paint_occluded(
            root,
            max_nodes=self.max_occlusion_nodes,
            descendants_occlude=self.descendants_occlude,
        )
        if occluded:
            logger.debug("Stage 3 (occlusion): marked %d occluded", len(occluded))

        # Stage 4: Bounds propagation — mark, don't remove.
        # Original uses excluded_by_parent flag: the node's tag is skipped in
        # serialization but children survive (so interactive children like <img>
        # with cursor:pointer still get indexed).
        bounds_excluded = propagate_parent_bounds(root)
        for node in bounds_excluded:
            node.should_display = False
        if bounds_excluded:
            logger.debug("Stage 4 (bounds): marked %d excluded", len(bounds_excluded))

        # Stage 5: Interactive index assignment.
        selector_map = _assign_interactive_indices(
            root,
            occluded=occluded,
            previous_selector_map=previous_selector_map,
            enable_compound=self.enable_compound_components,
        )
        logger.debug("Stage 5: %d interactive elements", len(selector_map))

        # Extract scroll info from the tab's own snapshot.
        scroll_info = _extract_scroll_info(capture.snapshot, viewport, device_pixel_ratio)

        return DOMState(
            dom_tree=root,
            selector_map=selector_map,
            scroll_info=scroll_info,
        )


# ---------------------------------------------------------------------------
# Phase 0: Parse CDP data → DOMElementNode tree
#
# Strategy (matching original browser-use), per CDP target:
#   1. DOMSnapshot → snapshot_lookup[backend_node_id] for bounds/styles/paint
#   2. Accessibility → ax_map[backend_node_id] for roles/names/properties
#   3. DOM.getDocument → recursive nested tree → DOMElementNode
# Each lookup is keyed by backend node id and therefore valid only inside
# the target it was captured from: an out-of-process frame is built from its
# own ``TargetCapture`` and grafted under its ``<iframe>``.
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

            # CDP DOMSnapshot returns clientRects and scrollRects already in CSS
            # pixels — only `bounds` is in device pixels. Do NOT divide these by
            # DPR or you get a half-size viewport that fails per-frame visibility
            # for the right half of the page (verified empirically on
            # xiaohongshu.com login modal: HTML clientRect was 640×290 instead
            # of 1280×581, dropping the entire right panel including the X
            # close button from selector_map).
            client_rect: DOMRect | None = None
            if li < len(layout_client_rects):
                cr = layout_client_rects[li]
                if cr and len(cr) >= 4:
                    client_rect = DOMRect(x=cr[0], y=cr[1], width=cr[2], height=cr[3])

            scroll_rects: DOMRect | None = None
            if li < len(layout_scroll_rects):
                sr = layout_scroll_rects[li]
                if sr and len(sr) >= 4:
                    scroll_rects = DOMRect(x=sr[0], y=sr[1], width=sr[2], height=sr[3])

            lookup[bid] = _SnapshotData(
                bounds=bounds, client_rect=client_rect, scroll_rects=scroll_rects,
                paint_order=paint_order, computed_styles=computed_styles, cursor_style=cursor_style,
            )

    return lookup


def _document_scroll_offsets(
    snapshot: dict[str, Any],
    device_pixel_ratio: float = 1.0,
) -> dict[str, tuple[float, float]]:
    """Frame id → the scroll offset of that frame's document, in CSS pixels.

    Like ``bounds``, ``scrollOffsetX/Y`` come back in device pixels.
    """
    dpr = device_pixel_ratio if device_pixel_ratio > 0 else 1.0
    strings = snapshot.get("strings", [])
    offsets: dict[str, tuple[float, float]] = {}
    for doc in snapshot.get("documents", []):
        frame_index = doc.get("frameId")
        if not isinstance(frame_index, int) or not 0 <= frame_index < len(strings):
            continue
        offsets[strings[frame_index]] = (
            doc.get("scrollOffsetX", 0.0) / dpr,
            doc.get("scrollOffsetY", 0.0) / dpr,
        )
    return offsets


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


@dataclass(frozen=True, slots=True)
class _Clip:
    """The visible region of one document, in page coordinates."""

    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True, slots=True)
class _Frame:
    """Where the document being built sits in the page.

    ``origin_*`` is the page position of the document's own ``(0, 0)`` —
    zero for the top-level document; for a frame's document, its owner's
    content box minus the document's scroll offset. ``clips`` are the visible
    regions of this document and of every document around it.
    ``in_frame`` is true inside any ``<iframe>`` / ``<frame>`` document.

    A translation, deliberately, even though a frame under a CSS transform
    is drawn scaled or rotated. ``DOMSnapshot`` reports axis-aligned
    rectangles, which cannot express a rotation, so the owner's true
    parallelogram is not knowable from this capture at all and a partial
    correction would be a guess. The consequence is bounded: these bounds
    decide visibility and occlusion, both of which carry a 1000px slack
    against outer viewports, and within one frame every box is measured in
    the same unscaled space, so the frame is self-consistent. Nothing that
    *acts* uses them — a click measures the element live and maps it
    through ``browser.geometry.FrameProjection``, which is exact.
    """

    origin_x: float = 0.0
    origin_y: float = 0.0
    clips: tuple[_Clip, ...] = ()
    frame_id: str | None = None
    in_frame: bool = False

    def to_page(self, rect: DOMRect | None) -> DOMRect | None:
        if rect is None:
            return None
        return DOMRect(
            x=rect.x + self.origin_x,
            y=rect.y + self.origin_y,
            width=rect.width,
            height=rect.height,
        )


@dataclass(slots=True)
class _Target:
    """The lookups of one captured CDP target."""

    capture: TargetCapture
    snapshot: dict[int, _SnapshotData]
    scroll_offsets: dict[str, tuple[float, float]]
    ax_map: dict[int, tuple[str, str, list[EnhancedAXProperty]]]
    js_listener_ids: set[int]
    device_pixel_ratio: float
    #: nodeId → built node. ``nodeId`` is per session, so per target.
    node_memo: dict[int, DOMElementNode] = field(default_factory=dict)

    @classmethod
    def of(cls, capture: TargetCapture, device_pixel_ratio: float) -> _Target:
        return cls(
            capture=capture,
            snapshot=_build_snapshot_lookup(capture.snapshot, device_pixel_ratio),
            scroll_offsets=_document_scroll_offsets(capture.snapshot, device_pixel_ratio),
            ax_map=_build_ax_map(capture.ax_tree),
            js_listener_ids=capture.js_click_listener_ids or set(),
            device_pixel_ratio=device_pixel_ratio,
        )


def _within_clips(
    bounds: DOMRect,
    clips: tuple[_Clip, ...],
    viewport_threshold: int = 1000,
) -> bool:
    """Whether ``bounds`` (page coordinates) shows through every enclosing document.

    Matches original ``is_element_visible_according_to_all_parents``: the
    element must overlap each document's viewport horizontally, and come
    within ``viewport_threshold`` of it vertically (content just below the
    fold is one scroll away).
    """
    for clip in clips:
        adj_x = bounds.x - clip.x
        adj_y = bounds.y - clip.y
        if not (
            adj_x < clip.width
            and adj_x + bounds.width > 0
            and adj_y < clip.height + viewport_threshold
            and adj_y + bounds.height > -viewport_threshold
        ):
            return False
    return True


def _css_px(value: str | None) -> float:
    """``"5px"`` → ``5.0``; anything unparseable → ``0.0``."""
    if not value:
        return 0.0
    try:
        return float(value.strip().removesuffix("px"))
    except ValueError:
        return 0.0


def _content_frame(
    owner: DOMElementNode,
    owner_bounds: DOMRect,
    around: _Frame,
    frame_id: str | None,
    scroll: tuple[float, float],
) -> _Frame:
    """The ``_Frame`` of the document shown by frame owner ``owner``.

    The document starts at the owner's content box — inside its border *and*
    padding — and is shifted by its own scroll offset. The document's own
    visible region is added when its ``<html>`` element is reached, because
    only that element knows the frame's viewport size.
    """
    styles = owner.computed_styles or {}
    content_x = owner_bounds.x + _css_px(styles.get("border-left-width")) + _css_px(
        styles.get("padding-left")
    )
    content_y = owner_bounds.y + _css_px(styles.get("border-top-width")) + _css_px(
        styles.get("padding-top")
    )
    return _Frame(
        origin_x=content_x - scroll[0],
        origin_y=content_y - scroll[1],
        clips=around.clips,
        frame_id=frame_id,
        in_frame=True,
    )


def _construct_node(
    node: dict[str, Any],
    target: _Target,
    frame: _Frame,
    *,
    include_shadow_dom: bool = True,
) -> DOMElementNode | None:
    """Recursively build DOMElementNode from DOM.getDocument node dict.

    Mirrors original browser-use ``_construct_enhanced_node`` (service.py:695-876),
    including its per-frame viewport visibility filtering. A frame owner's
    document is built into ``content_document`` whether ``node`` carries it
    (same-process frame) or ``target.capture.frames`` does (out-of-process
    frame, built from that target's own lookups). Shadow roots are built only
    with ``include_shadow_dom``.
    """
    ntype = node.get("nodeType", 1)

    def build(child: dict[str, Any], child_target: _Target, child_frame: _Frame) -> DOMElementNode | None:
        return _construct_node(
            child, child_target, child_frame, include_shadow_dom=include_shadow_dom
        )

    # DOCUMENT_NODE (9): skip wrapper, recurse children, return first result.
    if ntype == 9:
        for child in node.get("children", []):
            result = build(child, target, frame)
            if result is not None:
                return result
        return None

    # Only process ELEMENT (1), TEXT (3), DOCUMENT_FRAGMENT (11).
    if ntype not in (1, 3, 11):
        return None

    nid = node.get("nodeId", 0)
    if nid in target.node_memo:
        return target.node_memo[nid]

    bid: int | None = node.get("backendNodeId")
    snap = target.snapshot.get(bid) if bid else None
    capture = target.capture

    # TEXT_NODE (3).
    if ntype == 3:
        text_val = node.get("nodeValue", "") or ""
        text_bounds = frame.to_page(snap.bounds) if snap else None
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
            # Inside a frame, text answers to the frame's viewport and every
            # viewport around it, like the frame's elements do: a frame's
            # document is routinely many times taller than the box showing
            # it, and a frame far below the fold is not on the screen at all.
            # (Top-level text keeps the looser rule above.)
            if text_vis and frame.in_frame and text_bounds is not None:
                text_vis = _within_clips(text_bounds, frame.clips)
        text_node = DOMElementNode(
            tag_name="#text",
            node_type=3,
            node_value=text_val,
            node_id=nid,
            backend_node_id=bid,
            frame_id=frame.frame_id,
            target_id=capture.target_id,
            session_id=capture.session_id,
            bounds=text_bounds,
            is_visible=text_vis,
            _handle_namespace=capture.namespace,
        )
        target.node_memo[nid] = text_node
        return text_node

    # ELEMENT_NODE (1) or DOCUMENT_FRAGMENT_NODE (11).
    tag = node.get("nodeName", "").lower()
    node_name_upper = node.get("nodeName", "").upper()

    # An <html> element carries the id of the frame its document belongs to.
    if ntype == 1 and node_name_upper == "HTML" and node.get("frameId") is not None:
        frame = replace(frame, frame_id=node["frameId"])

    # Attributes (flat pairs in DOM.getDocument).
    attrs: dict[str, str] = {}
    raw_attrs = node.get("attributes", [])
    if raw_attrs:
        for j in range(0, len(raw_attrs) - 1, 2):
            attrs[raw_attrs[j]] = raw_attrs[j + 1]

    # Shadow root type.
    shadow_root_type: str | None = node.get("shadowRootType") or None

    bounds = frame.to_page(snap.bounds) if snap else None

    # Visibility: full per-frame check matching original.
    is_vis = _is_visible_by_css(snap) and bounds is not None and _within_clips(bounds, frame.clips)
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
    if bid and bid in target.ax_map:
        role_str, name_str, props_list = target.ax_map[bid]
        ax_role = role_str or None
        ax_name = name_str or None
        ax_properties = props_list if props_list else None

    dom_node = DOMElementNode(
        tag_name=tag,
        node_type=ntype,
        node_id=nid,
        backend_node_id=bid,
        frame_id=frame.frame_id,
        target_id=capture.target_id,
        session_id=capture.session_id,
        attributes=attrs,
        is_visible=is_vis,
        is_scrollable=is_scroll,
        bounds=bounds,
        client_rect=snap.client_rect if snap else None,
        scroll_rects=snap.scroll_rects if snap else None,
        computed_styles=snap.computed_styles if snap else None,
        cursor_style=snap.cursor_style if snap else None,
        paint_order=snap.paint_order if snap else None,
        ax_role=ax_role,
        ax_name=ax_name,
        ax_properties=ax_properties,
        shadow_root_type=shadow_root_type,
        has_js_click_listener=(bid in target.js_listener_ids) if bid else False,
        _handle_namespace=capture.namespace,
    )
    target.node_memo[nid] = dom_node

    # --- The frame descendants are built in (matching original lines 814-838) ---
    inner = frame

    # An <html> element with a frame id adds its document's viewport.
    if (
        ntype == 1
        and node_name_upper == "HTML"
        and node.get("frameId") is not None
        and snap is not None
        and snap.scroll_rects is not None
        and snap.client_rect is not None
    ):
        inner = replace(
            frame,
            clips=(
                *frame.clips,
                _Clip(
                    x=frame.origin_x + snap.scroll_rects.x,
                    y=frame.origin_y + snap.scroll_rects.y,
                    width=snap.client_rect.width,
                    height=snap.client_rect.height,
                ),
            ),
        )

    # --- Recurse children with updated frame tracking ---

    # Shadow roots.
    if node.get("shadowRoots") and include_shadow_dom:
        dom_node.shadow_roots = []
        for sr in node["shadowRoots"]:
            sr_node = build(sr, target, inner)
            if sr_node is not None:
                sr_node.parent = dom_node
                dom_node.shadow_roots.append(sr_node)

    # Content document (iframes): in this target, or in its own — and only
    # for a frame that is rendered. CSS visibility does not inherit across
    # the frame boundary, but painting and hit-testing stop there: a
    # ``visibility:hidden`` or unlaid-out iframe shows none of its document,
    # whose own styles still say visible, and a click aimed into it lands
    # on the page beneath. (browser-use builds a cross-origin frame's
    # document only for a visible owner too.)
    if node_name_upper in ("IFRAME", "FRAME") and is_vis and bounds is not None:
        child_frame_id: str | None = node.get("frameId")
        content: DOMElementNode | None = None
        if node.get("contentDocument"):
            scroll = target.scroll_offsets.get(child_frame_id or "", (0.0, 0.0))
            content = build(
                node["contentDocument"],
                target,
                _content_frame(dom_node, bounds, inner, child_frame_id, scroll),
            )
        elif child_frame_id is not None and child_frame_id in capture.frames:
            child_target = _Target.of(capture.frames[child_frame_id], target.device_pixel_ratio)
            child_root = child_target.capture.raw_dom.get("root")
            if child_root is not None:
                scroll = child_target.scroll_offsets.get(child_frame_id, (0.0, 0.0))
                content = build(
                    child_root,
                    child_target,
                    _content_frame(dom_node, bounds, inner, child_frame_id, scroll),
                )
        if content is not None:
            content.parent = dom_node
            dom_node.content_document = content

    # Children — skip shadow root nodeIds to avoid duplication.
    shadow_nids: set[int] = set()
    if node.get("shadowRoots"):
        for sr in node["shadowRoots"]:
            shadow_nids.add(sr.get("nodeId", -1))

    for child in node.get("children", []):
        if child.get("nodeId", -1) in shadow_nids:
            continue
        child_node = build(child, target, inner)
        if child_node is not None:
            child_node.parent = dom_node
            dom_node.children.append(child_node)

    return dom_node


def _build_tree_from_cdp(
    capture: TargetCapture,
    *,
    device_pixel_ratio: float = 1.0,
    include_shadow_dom: bool = True,
) -> DOMElementNode | None:
    """Merge 3 CDP protocol results into a unified DOMElementNode tree.

    Strategy (matching original browser-use):
    1. DOMSnapshot → snapshot_lookup[backend_node_id] for bounds/styles
    2. Accessibility → ax_map[backend_node_id] for ARIA data
    3. DOM.getDocument → recursive tree → DOMElementNode

    ``capture`` is the tab's target; out-of-process frames nested in it are
    built where their ``<iframe>`` is met.
    """
    root_data = capture.raw_dom.get("root")
    if root_data is None:
        return None
    return _construct_node(
        root_data,
        _Target.of(capture, device_pixel_ratio),
        _Frame(),
        include_shadow_dom=include_shadow_dom,
    )


# ---------------------------------------------------------------------------
# Stage 1: Simplify tree
# ---------------------------------------------------------------------------

def _simplify_tree(node: DOMElementNode) -> None:
    """Remove non-content elements in-place (depth-first, composed tree)."""
    node.children = [c for c in node.children if _should_keep(c)]
    for child in node.composed_children():
        _simplify_tree(child)


def _should_keep(node: DOMElementNode) -> bool:
    tag = node.tag_name.lower()
    if is_non_content_tag(tag):
        return False
    # SVG decorative children.
    parent_tag = node.parent.tag_name.lower() if node.parent else ""
    if parent_tag == "svg" and is_svg_decorative(tag):
        return False
    # Keep if visible, scrollable, has children, or hosts a shadow root or a
    # frame document.
    if is_visible(node):
        return True
    if node.is_scrollable:
        return True
    if node.children:
        return True
    if node.shadow_roots:
        return True
    if node.content_document is not None:
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
    """Remove structural-only nodes with no visible content (composed tree)."""
    for child in node.composed_children():
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
    if node.content_document is not None:
        return True
    return False


# ---------------------------------------------------------------------------
# Stage 3: Paint-order occlusion
# ---------------------------------------------------------------------------

def _mark_paint_occluded(
    root: DOMElementNode, *, max_nodes: int, descendants_occlude: bool = False
) -> set[int]:
    """Mark paint-occluded nodes ``should_display=False``; return their ``id()``s.

    Matching original browser-use behavior: occluded nodes keep their
    children in the tree (children float up in serialization) but the
    node's own tag is skipped and it doesn't get an interactive index.

    Occlusion is decided per document. A snapshot's paint orders are only
    comparable within one target, and a frame's content paints inside its
    owner's viewport while its page-space boxes run past it — comparing
    them with the surrounding document would let an iframe's white body
    "cover" whatever sits below the iframe. A frame hidden as a whole is
    handled at index time instead: its owner is occluded in *its* document.

    ``max_nodes`` budgets the whole composed tree, as the cleaner's knob is
    documented: past it, no document is filtered. Budgeting each document
    separately would let a page with many frames run the filter over several
    times the budget.

    Returned as object ids, not backend node ids: those repeat across targets.
    """
    documents: dict[tuple[str | None, str | None], list[DOMElementNode]] = {}
    candidates = 0
    for node in (root, *root.descendants()):
        documents.setdefault((node.session_id, node.frame_id), []).append(node)
        candidates += has_paint_geometry(node)
    if candidates > max_nodes:
        return set()

    occluded: set[int] = set()
    for nodes in documents.values():
        hidden = filter_by_paint_order(
            nodes, max_nodes=max_nodes, descendants_occlude=descendants_occlude
        )
        if not hidden:
            continue
        for node in nodes:
            if node.backend_node_id in hidden:
                node.should_display = False
                occluded.add(id(node))
    return occluded


# ---------------------------------------------------------------------------
# Stage 5: Interactive index assignment
# ---------------------------------------------------------------------------

def _assign_interactive_indices(
    root: DOMElementNode,
    *,
    occluded: set[int] | None = None,
    previous_selector_map: DOMSelectorMap | None = None,
    enable_compound: bool = True,
) -> DOMSelectorMap:
    """Walk the composed tree, detect interactive elements, build selector_map.

    Keys are ``element_handle(node._handle_namespace, node.backend_node_id)``
    — the backend node id itself for the tab's own target (matching
    browser-use), a namespaced key for an out-of-process frame's element,
    whose backend node id may equal one in the page. ``element_index``
    carries the same key, so a node says which index it answers to.

    The walk goes where ``TextSerializer`` goes — children, shadow roots, the
    frame document — so an element that reaches the prompt can be acted on.
    ``occluded`` holds the ``id()`` of paint-occluded nodes; the document of
    an occluded frame owner is not indexed, because nothing in it can be
    reached past whatever covers the frame.
    """
    _occluded = occluded or set()
    selector_map: DOMSelectorMap = {}

    def _walk(node: DOMElementNode, covered: bool) -> None:
        should_index = False

        # Nodes excluded by bounds propagation don't get indexed
        # (matching original excluded_by_parent behavior) but their
        # children are still walked. Paint-occluded nodes
        # (ignored_by_paint_order in original) are excluded the same way.
        if node.should_display and not covered:
            # Scrollable containers are indexed even if not in viewport
            # (agent can scroll the page to reach them).
            if is_scrollable_container(node):
                should_index = True
            # Everything else requires visibility.
            elif is_visible(node):
                if is_interactive(node) or is_dropdown_container(node):
                    node.is_interactive = True
                    should_index = True

        handle = (
            element_handle(node._handle_namespace, node.backend_node_id)
            if should_index and node.backend_node_id is not None
            else None
        )
        if handle is not None:
            node.element_index = handle
            selector_map[handle] = node

            # New node tracking.
            if previous_selector_map is not None and handle not in previous_selector_map:
                node.is_new = True

            # Compound components.
            if enable_compound:
                _add_compound_components(node)

        for child in node.children:
            _walk(child, covered)
        for shadow_root in node.shadow_roots or ():
            _walk(shadow_root, covered)
        if node.content_document is not None:
            _walk(node.content_document, covered or id(node) in _occluded)

    _walk(root, False)
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
