"""Verify BrowserUseCleaner correctly applies device_pixel_ratio.

Background: CDP ``DOMSnapshot.captureSnapshot`` returns ``bounds`` in
**device pixels** (already DPR-multiplied) but ``clientRects`` in CSS
pixels — an asymmetric quirk of the protocol. Every downstream consumer
(click, occlusion, serialization) assumes CSS pixels, so the cleaner
must divide bounds by DPR. Default DPR=1.0 must remain a no-op so the
existing test corpus and DPR-1 environments are unaffected.
"""

from __future__ import annotations

from typing import Any

from spectragent.core.capabilities.browser.dom.cleaning.browser_use import (
    REQUIRED_COMPUTED_STYLES,
    BrowserUseCleaner,
    _build_snapshot_lookup,
    _extract_scroll_info,
)
from spectragent.core.capabilities.browser.dom.models import TargetCapture


def _make_snapshot(
    bounds: list[float],
    client_rects: list[float],
    *,
    scroll_x: float = 0.0,
    scroll_y: float = 0.0,
) -> dict[str, Any]:
    """Build a minimal DOMSnapshot with a single layout node.

    The asymmetry is intentional: in real DPR=2 captures, ``bounds`` come
    out at 2x while ``clientRects`` stay 1x.
    """
    style_idx = [0] * len(REQUIRED_COMPUTED_STYLES)
    return {
        "strings": [""],
        "documents": [
            {
                "scrollOffsetX": scroll_x,
                "scrollOffsetY": scroll_y,
                "nodes": {"backendNodeId": [42]},
                "layout": {
                    "nodeIndex": [0],
                    "bounds": [bounds],
                    "clientRects": [client_rects],
                    "scrollRects": [],
                    "paintOrders": [0],
                    "styles": [style_idx],
                },
            }
        ],
    }


class TestSnapshotLookupDPR:
    def test_dpr_2_divides_only_bounds_not_client_rect(self) -> None:
        """``bounds`` are device pixels; ``clientRects`` are already CSS pixels.

        CDP DOMSnapshot returns these in different units — dividing both by
        DPR produces a half-size viewport and silently drops the right-half
        of the page from per-frame visibility checks.
        """
        snap = _make_snapshot(
            bounds=[200, 400, 100, 50],
            client_rects=[100, 200, 50, 25],
        )
        lookup = _build_snapshot_lookup(snap, device_pixel_ratio=2.0)
        data = lookup[42]

        # bounds: device → CSS via /dpr.
        assert data.bounds is not None
        assert (data.bounds.x, data.bounds.y) == (100.0, 200.0)
        assert (data.bounds.width, data.bounds.height) == (50.0, 25.0)

        # client_rect: already CSS, stays unchanged.
        assert data.client_rect is not None
        assert (data.client_rect.x, data.client_rect.y) == (100.0, 200.0)
        assert (data.client_rect.width, data.client_rect.height) == (50.0, 25.0)

    def test_dpr_1_passes_bounds_through_unchanged(self) -> None:
        snap = _make_snapshot(
            bounds=[200, 400, 100, 50],
            client_rects=[100, 200, 50, 25],
        )
        lookup = _build_snapshot_lookup(snap, device_pixel_ratio=1.0)
        data = lookup[42]

        assert data.bounds is not None
        assert (data.bounds.x, data.bounds.y) == (200.0, 400.0)
        assert (data.bounds.width, data.bounds.height) == (100.0, 50.0)

        assert data.client_rect is not None
        assert (data.client_rect.x, data.client_rect.y) == (100.0, 200.0)
        assert (data.client_rect.width, data.client_rect.height) == (50.0, 25.0)

    def test_zero_or_negative_dpr_falls_back_to_one(self) -> None:
        snap = _make_snapshot(
            bounds=[200, 400, 100, 50],
            client_rects=[100, 200, 50, 25],
        )
        lookup = _build_snapshot_lookup(snap, device_pixel_ratio=0.0)
        data = lookup[42]
        assert data.bounds is not None
        assert (data.bounds.x, data.bounds.y) == (200.0, 400.0)


class TestPerFrameVisibilityRegression:
    """Regression for the xiaohongshu login-modal X-button drop.

    Symptom: HTML root client_rect was wrongly halved (640 instead of 1280)
    because clientRects were /dpr'd. Modal right-panel at bounds.x=640
    failed the strict-less-than check `bounds.x < client_rect.width`,
    pulling the close button + phone input + login button out of
    selector_map.
    """

    def test_html_client_rect_preserved_at_dpr_2(self) -> None:
        # CDP-reported HTML root: bounds in device px, clientRect in CSS px.
        snap = _make_snapshot(
            bounds=[0, 0, 2560, 1162],  # device 2x of 1280×581
            client_rects=[0, 0, 1280, 581],  # CSS viewport
        )
        lookup = _build_snapshot_lookup(snap, device_pixel_ratio=2.0)
        data = lookup[42]
        assert data.bounds is not None and data.client_rect is not None
        assert (data.bounds.width, data.bounds.height) == (1280.0, 581.0)
        # Critical: stays at full-CSS 1280×581, not halved 640×290.5.
        assert (data.client_rect.width, data.client_rect.height) == (1280.0, 581.0)


class TestScrollInfoDPR:
    def test_scroll_offset_halved_at_dpr_2(self) -> None:
        snap = _make_snapshot(
            bounds=[0, 0, 100, 100],
            client_rects=[0, 0, 50, 50],
            scroll_x=400.0,
            scroll_y=600.0,
        )
        info = _extract_scroll_info(snap, viewport=(1280, 720), device_pixel_ratio=2.0)
        assert info.scroll_x == 200.0
        assert info.scroll_y == 300.0

    def test_scroll_offset_unchanged_at_dpr_1(self) -> None:
        snap = _make_snapshot(
            bounds=[0, 0, 100, 100],
            client_rects=[0, 0, 50, 50],
            scroll_x=400.0,
            scroll_y=600.0,
        )
        info = _extract_scroll_info(snap, viewport=(1280, 720), device_pixel_ratio=1.0)
        assert info.scroll_x == 400.0
        assert info.scroll_y == 600.0


class TestBrowserUseCleanerEndToEnd:
    async def test_clean_default_dpr_keeps_old_behavior(self) -> None:
        """Default kwarg=1.0 means every existing test stays green."""
        snap = _make_snapshot(
            bounds=[100, 200, 50, 25],
            client_rects=[100, 200, 50, 25],
        )
        cleaner = BrowserUseCleaner()
        state = await cleaner.clean(
            TargetCapture(raw_dom={"root": None}, snapshot=snap, ax_tree={"nodes": []}),
            (1280, 720),
        )
        # raw_dom has no root → empty state.
        assert state.dom_tree is None
        assert state.selector_map == {}

    async def test_clean_kwarg_overrides_default(self) -> None:
        # Minimal valid raw_dom — DOCUMENT_NODE wrapping an HTML element so
        # the cleaner produces a DOMState with scroll_info populated.
        raw_dom = {
            "root": {
                "nodeId": 1,
                "nodeType": 9,
                "nodeName": "#document",
                "children": [
                    {
                        "nodeId": 2,
                        "nodeType": 1,
                        "nodeName": "HTML",
                        "backendNodeId": 42,
                        "attributes": [],
                        "children": [],
                    },
                ],
            },
        }
        snap = _make_snapshot(
            bounds=[200, 400, 100, 50],
            client_rects=[100, 200, 50, 25],
            scroll_y=600.0,
        )
        cleaner = BrowserUseCleaner()
        state = await cleaner.clean(
            TargetCapture(raw_dom=raw_dom, snapshot=snap, ax_tree={"nodes": []}),
            (1280, 720),
            device_pixel_ratio=2.0,
        )
        assert state.scroll_info.scroll_y == 300.0
