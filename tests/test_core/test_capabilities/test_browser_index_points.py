"""Where a recorded element is clicked on its recorded screenshot (arch §4.15, ``index_points``).

Pure: a recorded ``DOMState`` and raw DOM documents in, a point or a reason
out. The page is the fixture site's ``sticky`` page
(``tests/_helpers/occlusion_pages.py``): a sticky header over links the page
scrolled underneath it.
"""

from __future__ import annotations

import pytest

from spectragent.core.capabilities.browser.dom.models import DOMRect, HitTest
from spectragent.core.capabilities.browser.index_points import (
    COORDINATE_COUNTERPARTS,
    ElementPoint,
    coordinate_action,
    locate_element,
)
from tests._helpers.occlusion_pages import (
    INDEX,
    LOADER_ID,
    Box,
    overlay,
    raw_document,
    sticky_state,
)


def _locate(name: str, **kwargs: object) -> ElementPoint:
    state = kwargs.pop("state", None) or sticky_state()
    return locate_element(INDEX[name], state, state.selector_map, **kwargs)  # type: ignore[arg-type]


class TestGeometry:
    def test_page_box_minus_scroll_centre_of_the_visible_part(self) -> None:
        located = _locate("clear", occlusion="none")
        # page (580, 700, 240, 40) - scroll (0, 500) -> viewport (580..820, 200..240)
        assert located.point == (700, 220)
        assert located.provenance["visible_rect"] == [580.0, 200.0, 820.0, 240.0]
        assert located.provenance["page_bbox"] == [580.0, 700.0, 240.0, 40.0]
        assert located.provenance["occlusion"] == "unchecked"
        assert located.provenance["evidence"] == "none"
        assert located.provenance["frame_clip"] == "none"

    def test_a_partly_visible_element_is_clicked_in_its_visible_part(self) -> None:
        state = sticky_state()
        state.selector_map[INDEX["clear"]].bounds = DOMRect(1200, 1150, 200, 100)
        located = locate_element(INDEX["clear"], state, state.selector_map, occlusion="none")
        # viewport (1200..1400, 650..750) clipped to 1280x720 -> (1200..1280, 650..720)
        assert located.point == (1240, 685)

    def test_off_screen_is_target_offscreen(self) -> None:
        located = _locate("below", occlusion="check")
        assert (located.point, located.failure) == (None, "target_offscreen")
        assert "[9]" in located.detail

    def test_a_smaller_screenshot_viewport_clips_too(self) -> None:
        located = _locate("clear", occlusion="none", viewport=(640, 360))
        # (580..820, 200..240) clipped to 640x360 -> (580..640, 200..240)
        assert located.point == (610, 220)

    def test_an_index_missing_from_the_selector_map(self) -> None:
        state = sticky_state()
        located = locate_element(12345, state, state.selector_map)
        assert (located.point, located.failure) == (None, "no_counterpart")
        assert "[12345]" in located.detail

    def test_a_same_process_iframe_element_is_clipped_by_its_owner(self) -> None:
        state = sticky_state()
        inside = state.selector_map[INDEX["inside"]]
        inside.bounds = DOMRect(1100, 650, 100, 30)  # past the owner's right edge (1160)
        located = locate_element(INDEX["inside"], state, state.selector_map, occlusion="none")
        assert located.point == (1130, 165)
        assert located.provenance["frame_clip"] == "applied"

    def test_an_element_missing_from_the_tree_is_clipped_by_the_viewport_only(self) -> None:
        state = sticky_state()
        entry = state.selector_map[INDEX["inside"]]
        entry.bounds = DOMRect(1100, 650, 100, 30)
        assert state.dom_tree is not None
        # what a depth-truncated encoding leaves: the entry, not its place in the tree
        frame = next(n for n in state.dom_tree.descendants() if n.attributes.get("id") == "frame")
        assert frame.content_document is not None
        frame.content_document.children.clear()
        located = locate_element(INDEX["inside"], state, state.selector_map, occlusion="none")
        assert located.point == (1150, 165)
        assert located.provenance["frame_clip"] == "unknown"

    def test_coordinate_counterparts(self) -> None:
        assert dict(COORDINATE_COUNTERPARTS) == {
            "browser.click": "browser.click_coordinate",
            "browser.type": "browser.type_coordinate",
        }
        assert coordinate_action(
            "browser.type", {"index": 6, "text": "a", "clear": False}, (1, 2)
        ) == (
            "browser.type_coordinate",
            {"coordinate_x": 1, "coordinate_y": 2, "text": "a", "clear": False},
        )
        with pytest.raises(KeyError):
            coordinate_action("browser.select_dropdown", {"index": 1, "text": "a"}, (1, 2))


class TestHitTestEvidence:
    def test_a_centre_hit_is_the_centre(self) -> None:
        state = sticky_state(paint_orders=False, hit_tests={"clear": HitTest(True, (700, 220))})
        located = _locate("clear", state=state)
        assert located.point == (700, 220)
        assert (located.provenance["point_source"], located.provenance["evidence"]) == (
            "center",
            "hit_test",
        )

    def test_a_covered_centre_uses_the_visible_point(self) -> None:
        state = sticky_state(
            paint_orders=False, hit_tests={"partial": HitTest(False, (302.0, 114.0))}
        )
        located = _locate("partial", state=state)
        assert located.point == (302, 114)
        assert located.provenance["point_source"] == "hit_test"

    def test_no_hit_anywhere_is_occluded(self) -> None:
        state = sticky_state(paint_orders=False, hit_tests={"covered": HitTest(False, None)})
        located = _locate("covered", state=state)
        assert (located.point, located.failure) == (None, "occluded")
        assert located.provenance["evidence"] == "hit_test"


class TestDomRawEvidence:
    @pytest.mark.parametrize("dpr", [1.0, 2.0])
    def test_an_uncovered_centre(self, dpr: float) -> None:
        located = _locate(
            "clear",
            state=sticky_state(paint_orders=False),
            documents=[raw_document(dpr=dpr)],
            loader_id=LOADER_ID,
        )
        assert located.point == (700, 220)
        assert located.provenance["evidence"] == "dom_raw"

    @pytest.mark.parametrize("dpr", [1.0, 2.0])
    def test_a_sticky_header_over_the_centre_moves_the_point_to_the_grid(self, dpr: float) -> None:
        located = _locate(
            "partial",
            state=sticky_state(paint_orders=False),
            documents=[raw_document(dpr=dpr)],
            loader_id=LOADER_ID,
        )
        # visible (300..540, 40..140); grid rows 42, 66, 90, 114, 138 — the first
        # below the header (viewport y >= 100) is 114, its first column 302
        assert located.point == (302, 114)
        assert located.provenance["point_source"] == "grid"
        assert located.provenance["occlusion"] == "clear"

    def test_fully_covered_is_occluded(self) -> None:
        located = _locate(
            "covered",
            state=sticky_state(paint_orders=False),
            documents=[raw_document()],
            loader_id=LOADER_ID,
        )
        assert (located.failure, located.provenance["evidence"]) == ("occluded", "dom_raw")

    def test_a_click_through_overlay_covers_nothing(self) -> None:
        state = sticky_state(paint_orders=False, extra=(overlay(),))
        documents = [raw_document(extra=(overlay(),), click_through=("overlay",))]
        assert _locate("clear", state=state, documents=documents).point == (700, 220)
        blocking = [raw_document(extra=(overlay(),))]
        assert _locate("clear", state=state, documents=blocking).failure == "occluded"

    def test_the_document_of_the_steps_stamp_is_used(self) -> None:
        other = raw_document(loader_id="another-document", extra=(overlay(),))
        located = _locate(
            "clear",
            state=sticky_state(paint_orders=False),
            documents=[other, raw_document()],
            loader_id=LOADER_ID,
        )
        assert located.point == (700, 220)

    def test_two_documents_and_no_stamp_is_no_evidence(self) -> None:
        located = _locate(
            "clear",
            state=sticky_state(paint_orders=False),
            documents=[raw_document(loader_id="a"), raw_document(loader_id="b")],
            loader_id=None,
        )
        assert located.failure == "occlusion_unknown"

    def test_an_iframe_element_is_not_judged_by_the_main_document(self) -> None:
        located = _locate(
            "inside", state=sticky_state(paint_orders=False), documents=[raw_document()]
        )
        assert located.failure == "occlusion_unknown"


class TestTracedPaintOrderEvidence:
    def test_a_partial_cover_moves_the_point_to_the_grid(self) -> None:
        located = _locate("partial")
        assert located.point == (302, 114)
        assert (located.provenance["point_source"], located.provenance["evidence"]) == (
            "grid",
            "paint_order",
        )

    def test_clear_and_covered(self) -> None:
        assert _locate("clear").point == (700, 220)
        assert _locate("covered").failure == "occluded"

    def test_an_iframe_element_is_judged_in_its_own_document(self) -> None:
        """The header's paint order is the main document's: it does not cover frame content."""
        located = _locate("inside")
        assert located.point == (892, 116)
        assert located.provenance["evidence"] == "paint_order"

    def test_a_descendant_painted_on_top_is_the_element_a_stranger_is_not(self) -> None:
        """A click on a descendant reaches the element; on anything else it does not."""
        child = Box("partial_text", 40, (300, 590, 240, 50), 50, "partial", indexed=False)
        assert _locate("partial", state=sticky_state(extra=(child,))).point == (420, 90)
        stranger = Box("partial_text", 40, (300, 590, 240, 50), 50, "body", indexed=False)
        assert _locate("partial", state=sticky_state(extra=(stranger,))).failure == "occluded"


def test_no_evidence_is_occlusion_unknown_and_off_checks_nothing() -> None:
    state = sticky_state(paint_orders=False)
    unknown = _locate("covered", state=state)
    assert (unknown.point, unknown.failure) == (None, "occlusion_unknown")
    assert unknown.provenance["evidence"] == "none"
    off = _locate("covered", state=state, occlusion="none")
    assert off.point == (140, 50)
    assert off.provenance["occlusion"] == "unchecked"
