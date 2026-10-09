"""The composed page: frame and shadow-root content reaches the selector map.

``TextSerializer`` has always printed shadow-root and iframe content, while
index assignment walked ``children`` only — so those elements reached the
prompt with no ``[index]`` and could not be acted on. And content of a
cross-origin (out-of-process) iframe was not captured at all.

Most tests here run the ``browser_use`` cleaner over
``tests._helpers.dom_payloads.reference_page`` — one of everything, with the
geometry chosen so every term of the page-space conversion shows in the
numbers (see its docstring for the layout).
"""

from __future__ import annotations

import json
import re

import pytest

import spectragent.core.capabilities.browser.dom.serializers.html_serializer  # noqa: F401
from spectragent.core.capabilities.browser.dom.models import (
    FRAME_HANDLE_BASE,
    DOMElementNode,
    DOMState,
    TargetCapture,
    element_handle,
)
from spectragent.core.capabilities.browser.dom.serializers.html_serializer import HTMLSerializer
from spectragent.core.capabilities.browser.observations import DOMObs, _node_to_json
from tests._helpers.dom_payloads import (
    CROSS,
    NESTED,
    TINY,
    Doc,
    El,
    ax_node,
    by_id,
    clean,
    compile_target,
    indexed_ids,
    reference_page,
)


class TestEveryDocumentIsIndexed:
    async def test_same_target_frame_shadow_root_and_frame_targets(self) -> None:
        state = await clean(reference_page())
        for element_id in (
            "main-btn",
            "same-btn",
            "shadow-btn",
            "cb",
            "cross-input",
            "nested-btn",
            "anchor",
        ):
            by_id(state, element_id)

    async def test_each_element_keeps_the_session_that_owns_it(self) -> None:
        state = await clean(reference_page())
        assert by_id(state, "main-btn")[1].session_id == "tab"
        assert by_id(state, "same-btn")[1].session_id == "tab"
        assert by_id(state, "shadow-btn")[1].session_id == "tab"
        assert by_id(state, "cb")[1].session_id == "cross"
        assert by_id(state, "nested-btn")[1].session_id == "nested"

        cb = by_id(state, "cb")[1]
        assert (cb.target_id, cb.frame_id) == ("CROSS", "CROSS")
        same = by_id(state, "same-btn")[1]
        assert (same.target_id, same.frame_id) == (None, "SAME")

    async def test_the_frame_document_hangs_under_its_owner(self) -> None:
        state = await clean(reference_page())
        _, cb = by_id(state, "cb")
        _, nested = by_id(state, "nested-btn")
        assert [o.attributes["id"] for o in cb.frame_owners()] == ["cross"]
        assert [o.attributes["id"] for o in nested.frame_owners()] == ["nested", "cross"]
        assert nested.frame_owners()[0].session_id == "cross"  # the owner lives in its parent
        assert by_id(state, "main-btn")[1].frame_owners() == []


class TestKeysDoNotCollideAcrossTargets:
    async def test_the_tab_keeps_backend_ids_and_frame_targets_get_namespaces(self) -> None:
        state = await clean(reference_page())
        assert by_id(state, "main-btn")[0] == 10
        assert by_id(state, "same-btn")[0] == 52
        assert by_id(state, "shadow-btn")[0] == 14
        # Backend id 10 three more times — in three other targets.
        assert by_id(state, "cb")[0] == CROSS * FRAME_HANDLE_BASE + 10
        assert by_id(state, "nested-btn")[0] == NESTED * FRAME_HANDLE_BASE + 10
        assert by_id(state, "anchor")[0] == TINY * FRAME_HANDLE_BASE + 10

    async def test_element_index_is_the_key(self) -> None:
        state = await clean(reference_page())
        assert state.selector_map
        for key, node in state.selector_map.items():
            assert node.element_index == key
            assert element_handle(node._handle_namespace, node.backend_node_id or -1) == key

    def test_a_backend_id_outside_the_namespace_gets_no_key(self) -> None:
        assert element_handle(0, FRAME_HANDLE_BASE) is None
        assert element_handle(3, FRAME_HANDLE_BASE - 1) == 4 * FRAME_HANDLE_BASE - 1

    async def test_keys_are_stable_across_observations(self) -> None:
        first = await clean(reference_page())
        second = await clean(reference_page(), previous=first.selector_map)
        assert set(second.selector_map) == set(first.selector_map)
        assert not [n for n in second.selector_map.values() if n.is_new]

    async def test_is_new_is_decided_by_key(self) -> None:
        first = await clean(reference_page())
        del first.selector_map[by_id(first, "cb")[0]]
        second = await clean(reference_page(), previous=first.selector_map)
        new = {n.attributes.get("id") for n in second.selector_map.values() if n.is_new}
        # main-btn shares cb's backend id; only the frame's element is new.
        assert new == {"cb"}


class TestPageSpaceBounds:
    async def test_same_target_frame_content_is_offset_by_border_padding_and_scroll(self) -> None:
        state = await clean(reference_page())
        bounds = by_id(state, "same-btn")[1].bounds
        assert bounds is not None
        # owner x 100 + border 5 + padding 7; owner y 300 + 5 + 7 - scroll 40 + 102.
        assert (bounds.x, bounds.y) == (112.0, 374.0)

    async def test_frame_target_content_is_offset_the_same_way(self) -> None:
        state = await clean(reference_page())
        bounds = by_id(state, "cb")[1].bounds
        assert bounds is not None
        # owner (40, 600) + border 4 + padding 6, frame scrolled 10.
        assert (bounds.x, bounds.y) == (50.0, 650.0)

    async def test_nested_frames_accumulate(self) -> None:
        state = await clean(reference_page())
        bounds = by_id(state, "nested-btn")[1].bounds
        assert bounds is not None
        # cross content origin (50, 600); #nested at (0, 120) in it; + border 3
        # + padding 2; button at (5, 7) in the nested document.
        assert (bounds.x, bounds.y) == (60.0, 732.0)

    async def test_main_document_bounds_are_unchanged(self) -> None:
        state = await clean(reference_page())
        bounds = by_id(state, "main-btn")[1].bounds
        assert bounds is not None
        assert (bounds.x, bounds.y, bounds.width, bounds.height) == (0.0, 200.0, 80.0, 20.0)


class TestFrameOwners:
    async def test_a_small_frame_does_not_hide_its_content(self) -> None:
        state = await clean(reference_page())
        by_id(state, "anchor")
        assert "tiny" not in indexed_ids(state)

    async def test_a_small_frame_whose_document_is_in_the_tree_is_not_a_target(self) -> None:
        """Its elements are: a 304x78 widget's centre is not its checkbox."""
        state = await clean(reference_page())
        assert {"tiny", "nested"}.isdisjoint(indexed_ids(state))

    async def test_a_large_frame_is_a_target_as_well_as_its_content(self) -> None:
        """browser-use's rule, kept: at 100x100 the frame is a handle of its own."""
        state = await clean(reference_page())
        assert by_id(state, "same")[0] == 11
        assert by_id(state, "cross")[0] == 15
        assert {"same-btn", "cb", "cross-input"} <= indexed_ids(state)

    async def test_a_frame_whose_document_was_not_captured_is_a_target(self) -> None:
        state = await clean(reference_page())
        key, node = by_id(state, "opaque")
        assert key == 17
        assert node.content_document is None

    async def test_an_editor_frame_can_be_addressed_through_its_owner(self) -> None:
        """A rich-text editor whose document *is* the editable surface.

        ``<body contenteditable>`` is never indexed (html/body never are), and
        the frame holds nothing else — so the owner is the only index an
        agent can type into, as TinyMCE / CKEditor 4 iframes need.
        """
        editor = compile_target(
            Doc(
                "EDITOR",
                html_id=1,
                viewport=(400, 200),
                height=200,
                body_attrs={"contenteditable": "true"},
                body=[El("#text", 5, bounds=(0, 0, 100, 18), text="hello rich text")],
            ),
            session_id="editor",
            target_id="EDITOR",
            namespace=1,
        )
        page = compile_target(
            Doc(
                "MAIN",
                html_id=1,
                body=[
                    El(
                        "iframe",
                        20,
                        bounds=(0, 40, 400, 200),
                        attrs={"id": "ed"},
                        oopif_frame_id="EDITOR",
                    )
                ],
            ),
            session_id="tab",
            frames={"EDITOR": editor},
        )
        state = await clean(page)
        assert set(state.selector_map) == {20}
        lines = state.serialized_dom.splitlines()
        assert lines[0] == "[20]<iframe id=ed />"
        assert lines[1] == "\thello rich text"

    async def test_content_outside_its_frame_viewport_is_not_indexed(self) -> None:
        """Visible in its own document's coordinates, outside the frame showing it.

        Its twin ``#same-btn`` 400 px to the left is indexed, so what drops it
        is the frame's viewport — checked in page space — and nothing else.
        """
        state = await clean(reference_page())
        by_id(state, "same-btn")
        assert "same-clipped" not in indexed_ids(state)
        assert "same-clipped" not in state.serialized_dom

    async def test_an_occluded_frame_owner_hides_its_document(self) -> None:
        state = await clean(reference_page(overlay=True))
        ids = indexed_ids(state)
        assert "cb" not in ids and "nested-btn" not in ids and "cross" not in ids
        # The modal covers only the cross frame; the rest of the page stays.
        assert {"main-btn", "same-btn", "shadow-btn"} <= ids

    async def test_frame_content_never_occludes_the_page_around_it(self) -> None:
        """A frame's document is compared only with itself.

        Its white body is 800 px tall in page space while the frame shows 200;
        judged against the page it would bury whatever sits below the frame.
        """
        capture = reference_page()
        cross_html = capture.frames["CROSS"].snapshot["documents"][0]
        cross_html["layout"]["paintOrders"] = [99] * len(cross_html["layout"]["paintOrders"])
        state = await clean(capture)
        by_id(state, "anchor")  # tiny frame sits right below the cross frame
        by_id(state, "opaque")


def _page_with_frame(
    owner_styles: dict[str, str],
    *,
    bounds: tuple[float, float, float, float] | None = (0, 100, 200, 60),
) -> TargetCapture:
    """A page button beneath a cross-origin frame whose owner has ``owner_styles``."""
    frame = compile_target(
        Doc(
            "GHOST",
            html_id=1,
            viewport=(200, 60),
            height=60,
            body=[
                El(
                    "button",
                    10,
                    bounds=(0, 0, 200, 60),
                    attrs={"id": "ghost"},
                    children=[El("#text", 11, bounds=(80, 20, 40, 18), text="ghost text")],
                )
            ],
        ),
        session_id="ghost",
        target_id="GHOST",
        namespace=1,
    )
    return compile_target(
        Doc(
            "MAIN",
            html_id=1,
            body=[
                El("button", 10, bounds=(60, 115, 80, 30), attrs={"id": "under"}),
                El(
                    "iframe",
                    20,
                    bounds=bounds,
                    attrs={"id": "owner"},
                    styles=owner_styles,
                    oopif_frame_id="GHOST",
                    paint_order=3,
                ),
            ],
        ),
        session_id="tab",
        frames={"GHOST": frame},
    )


def _owner(state: DOMState, element_id: str) -> DOMElementNode | None:
    assert state.dom_tree is not None
    for node in (state.dom_tree, *state.dom_tree.descendants()):
        if node.tag_name == "iframe" and node.attributes.get("id") == element_id:
            return node
    return None


class TestAFrameIsOnlyAsVisibleAsItsOwner:
    """CSS visibility does not inherit across the frame boundary; painting does.

    A ``visibility:hidden`` iframe paints nothing and hit-tests to what is
    beneath it, while its document's own computed styles still say visible —
    so an index there was a click that landed on the page underneath. A frame
    that is not rendered contributes no document at all.
    """

    @pytest.mark.parametrize(
        "owner_styles",
        [{"visibility": "hidden"}, {"opacity": "0"}, {"display": "none"}],
        ids=["visibility-hidden", "opacity-0", "display-none"],
    )
    async def test_a_hidden_owner_shows_no_document(self, owner_styles: dict[str, str]) -> None:
        state = await clean(_page_with_frame(owner_styles))
        assert indexed_ids(state) == {"under"}
        assert "ghost" not in state.serialized_dom
        owner = _owner(state, "owner")
        assert owner is None or owner.content_document is None

    async def test_an_owner_without_layout_shows_no_document(self) -> None:
        state = await clean(_page_with_frame({}, bounds=None))
        assert indexed_ids(state) == {"under"}
        assert "ghost" not in state.serialized_dom

    async def test_an_owner_scrolled_far_out_of_view_shows_no_document(self) -> None:
        state = await clean(_page_with_frame({}, bounds=(0, 5000, 200, 60)))
        assert indexed_ids(state) == {"under"}
        assert "ghost" not in state.serialized_dom

    async def test_a_visible_owner_shows_it(self) -> None:
        state = await clean(_page_with_frame({}))
        assert {"under", "ghost"} <= indexed_ids(state)
        assert "ghost text" in state.serialized_dom

    async def test_an_unrendered_frame_inside_a_frame_adds_nothing(self) -> None:
        """Ad frames nest unrendered frames by the hundred (measured: 206 of 214
        on one ad-heavy page), each captured, each once printed as an empty
        ``|IFRAME|`` line."""
        tracker = compile_target(
            Doc(
                "TRACKER",
                html_id=1,
                viewport=(300, 250),
                height=250,
                body=[El("#text", 10, bounds=(0, 0, 100, 18), text="beacon")],
            ),
            session_id="tracker",
            target_id="TRACKER",
            namespace=2,
        )
        ad = compile_target(
            Doc(
                "AD",
                html_id=1,
                viewport=(300, 250),
                height=250,
                body=[
                    El("a", 10, bounds=(0, 0, 300, 250), attrs={"id": "ad-link", "href": "/x"}),
                    El("iframe", 11, attrs={"id": "tracker"}, oopif_frame_id="TRACKER"),
                ],
            ),
            session_id="ad",
            target_id="AD",
            namespace=1,
            frames={"TRACKER": tracker},
        )
        page = compile_target(
            Doc(
                "MAIN",
                html_id=1,
                body=[
                    El(
                        "iframe",
                        20,
                        bounds=(0, 0, 300, 250),
                        attrs={"id": "ad"},
                        oopif_frame_id="AD",
                    )
                ],
            ),
            session_id="tab",
            frames={"AD": ad},
        )
        state = await clean(page)
        assert "ad-link" in indexed_ids(state)
        assert "tracker" not in state.serialized_dom
        assert "beacon" not in state.serialized_dom
        tracker_owner = _owner(state, "tracker")
        assert tracker_owner is None or tracker_owner.content_document is None

    async def test_hidden_is_inherited_by_nested_frames(self) -> None:
        inner = compile_target(
            Doc(
                "INNER",
                html_id=1,
                viewport=(100, 40),
                height=40,
                body=[El("button", 10, bounds=(0, 0, 50, 20), attrs={"id": "inner-btn"})],
            ),
            session_id="inner",
            target_id="INNER",
            namespace=2,
        )
        outer = compile_target(
            Doc(
                "OUTER",
                html_id=1,
                viewport=(200, 100),
                height=100,
                body=[El("iframe", 12, bounds=(0, 0, 100, 40), oopif_frame_id="INNER")],
            ),
            session_id="outer",
            target_id="OUTER",
            namespace=1,
            frames={"INNER": inner},
        )
        page = compile_target(
            Doc(
                "MAIN",
                html_id=1,
                body=[
                    El(
                        "iframe",
                        20,
                        bounds=(0, 0, 200, 100),
                        styles={"visibility": "hidden"},
                        oopif_frame_id="OUTER",
                    )
                ],
            ),
            session_id="tab",
            frames={"OUTER": outer},
        )
        state = await clean(page)
        assert state.selector_map == {}


class TestFrameText:
    """A frame's text answers to the viewports it is shown through, like its elements."""

    @staticmethod
    def page(frame_top: float) -> TargetCapture:
        long_document = compile_target(
            Doc(
                "LONG",
                html_id=1,
                viewport=(300, 250),
                height=6000,
                body=[
                    El("#text", 10, bounds=(0, 10, 200, 18), text="first paragraph"),
                    El("#text", 11, bounds=(0, 5000, 200, 18), text="deep paragraph"),
                ],
            ),
            session_id="long",
            target_id="LONG",
            namespace=1,
        )
        return compile_target(
            Doc(
                "MAIN",
                html_id=1,
                height=9000,
                body=[
                    El("#text", 30, bounds=(0, 8000, 200, 18), text="main text far below"),
                    El("iframe", 20, bounds=(0, frame_top, 300, 250), oopif_frame_id="LONG"),
                ],
            ),
            session_id="tab",
            frames={"LONG": long_document},
        )

    async def test_text_scrolled_out_of_the_frame_is_not_printed(self) -> None:
        state = await clean(self.page(frame_top=100))
        assert "first paragraph" in state.serialized_dom
        assert "deep paragraph" not in state.serialized_dom

    async def test_a_frame_far_below_the_fold_prints_nothing(self) -> None:
        state = await clean(self.page(frame_top=6000))
        assert "first paragraph" not in state.serialized_dom

    async def test_top_level_text_keeps_its_rule(self) -> None:
        """Out of scope here: the page's own text is still printed wherever it is."""
        state = await clean(self.page(frame_top=6000))
        assert "main text far below" in state.serialized_dom


class TestCleanerOptions:
    async def test_shadow_dom_can_be_left_out(self) -> None:
        state = await clean(reference_page(), include_shadow_dom=False)
        assert "shadow-btn" not in indexed_ids(state)
        assert "shadow-btn" not in state.serialized_dom
        assert {"main-btn", "cb"} <= indexed_ids(state)

    @staticmethod
    def covered_in_page_and_frame() -> TargetCapture:
        """One element under an opaque overlay in the page, one in a frame."""
        frame = compile_target(
            Doc(
                "FRAME",
                html_id=1,
                viewport=(300, 100),
                height=100,
                body=[
                    El("button", 10, bounds=(0, 0, 50, 20), attrs={"id": "frame-under"}),
                    El(
                        "div",
                        11,
                        bounds=(0, 0, 300, 100),
                        styles={"background-color": "rgb(0, 0, 0)"},
                        paint_order=5,
                    ),
                ],
            ),
            session_id="frame",
            target_id="FRAME",
            namespace=1,
        )
        return compile_target(
            Doc(
                "MAIN",
                html_id=1,
                body=[
                    El("button", 10, bounds=(0, 0, 50, 20), attrs={"id": "page-under"}),
                    El(
                        "div",
                        11,
                        bounds=(0, 0, 400, 40),
                        styles={"background-color": "rgb(0, 0, 0)"},
                        paint_order=5,
                    ),
                    El("iframe", 12, bounds=(0, 300, 300, 100), oopif_frame_id="FRAME"),
                ],
            ),
            session_id="tab",
            frames={"FRAME": frame},
        )

    async def test_occlusion_runs_when_the_whole_tree_is_within_budget(self) -> None:
        state = await clean(self.covered_in_page_and_frame(), max_occlusion_nodes=100)
        assert {"page-under", "frame-under"}.isdisjoint(indexed_ids(state))

    async def test_the_occlusion_budget_counts_every_document_together(self) -> None:
        """Each document alone is within 8 nodes; the tree is not — nothing is filtered."""
        capture = self.covered_in_page_and_frame()
        per_document = [
            len(doc["layout"]["paintOrders"])
            for target in (capture, capture.frames["FRAME"])
            for doc in target.snapshot["documents"]
        ]
        assert max(per_document) <= 8 < sum(per_document)

        state = await clean(capture, max_occlusion_nodes=8)
        assert {"page-under", "frame-under"} <= indexed_ids(state)

    @staticmethod
    def anchor_under_its_own_face() -> TargetCapture:
        """reCAPTCHA's shape: a checkbox whose opaque face is its own child."""
        widget = compile_target(
            Doc(
                "WIDGET",
                html_id=1,
                viewport=(304, 78),
                height=78,
                body=[
                    El(
                        "span",
                        10,
                        bounds=(11, 25, 28, 28),
                        attrs={"id": "anchor", "role": "checkbox", "tabindex": "0"},
                        paint_order=4,
                        children=[
                            El(
                                "div",
                                11,
                                bounds=(11, 25, 28, 28),
                                attrs={"role": "presentation"},
                                styles={"background-color": "rgb(255, 255, 255)"},
                                paint_order=10,
                            )
                        ],
                    )
                ],
            ),
            session_id="widget",
            target_id="WIDGET",
            namespace=1,
        )
        return compile_target(
            Doc(
                "MAIN",
                html_id=1,
                body=[El("iframe", 20, bounds=(490, 250, 304, 78), oopif_frame_id="WIDGET")],
            ),
            session_id="tab",
            frames={"WIDGET": widget},
        )

    async def test_by_default_a_control_is_not_hidden_by_its_own_face(self) -> None:
        state = await clean(self.anchor_under_its_own_face())
        assert by_id(state, "anchor")[0] == FRAME_HANDLE_BASE + 10

    async def test_upstream_occlusion_is_one_option_away(self) -> None:
        state = await clean(self.anchor_under_its_own_face(), descendants_occlude=True)
        assert "anchor" not in indexed_ids(state)


class TestWhatTheModelReads:
    async def test_frame_content_is_printed_with_the_key_the_map_answers_to(self) -> None:
        state = await clean(reference_page())
        lines = state.serialized_dom.splitlines()
        cb_key = by_id(state, "cb")[0]
        nested_key = by_id(state, "nested-btn")[0]

        cross_line = next(i for i, line in enumerate(lines) if "<iframe id=cross" in line)
        assert lines[cross_line].lstrip("\t").startswith("[15]<iframe")
        cb_line = next(i for i, line in enumerate(lines) if "id=cb" in line)
        assert lines[cb_line].strip().startswith(f"[{cb_key}]<div")
        assert cb_line > cross_line
        depth = len(lines[cross_line]) - len(lines[cross_line].lstrip("\t"))
        assert len(lines[cb_line]) - len(lines[cb_line].lstrip("\t")) == depth + 1
        assert any(line.strip().startswith(f"[{nested_key}]<button") for line in lines)

    async def test_a_frame_that_is_not_a_target_is_printed_as_a_frame(self) -> None:
        state = await clean(reference_page())
        tiny = next(line for line in state.serialized_dom.splitlines() if "<iframe id=tiny" in line)
        assert tiny.strip().startswith("|IFRAME|")

    async def test_an_uncaptured_frame_is_printed_as_a_target(self) -> None:
        state = await clean(reference_page())
        assert any(
            line.strip().startswith("[17]<iframe") for line in state.serialized_dom.splitlines()
        )

    async def test_every_printed_index_resolves(self) -> None:
        state = await clean(reference_page())
        printed = {int(m) for m in re.findall(r"\[(\d+)\]<", state.serialized_dom)}
        assert printed == set(state.selector_map)

    async def test_html_serializer_reaches_frame_content_too(self) -> None:
        state = await clean(reference_page())
        html = HTMLSerializer().serialize(state)
        assert f'data-idx="{by_id(state, "cb")[0]}"' in html
        assert f'data-idx="{by_id(state, "shadow-btn")[0]}"' in html


class TestWhatTheTraceRecords:
    async def test_frame_subtrees_are_recorded_under_their_owner(self) -> None:
        state = await clean(reference_page())
        payload = json.loads(
            DOMObs(element_tree=state, selector_map=dict(state.selector_map)).model_dump_json()
        )
        raw = json.dumps(payload["element_tree"])
        assert '"cross-input"' in raw and '"nested-btn"' in raw and '"shadow-btn"' in raw

    async def test_selector_map_entries_name_their_key_and_session(self) -> None:
        state = await clean(reference_page())
        payload = json.loads(
            DOMObs(element_tree=state, selector_map=dict(state.selector_map)).model_dump_json()
        )
        entries = payload["selector_map"]
        assert set(entries) == {str(k) for k in state.selector_map}
        cb_key = by_id(state, "cb")[0]
        # ``#cross`` is indexed too and its entry records its document — and
        # cb's own entry is still written in full, not as a pointer into it.
        cb = entries[str(cb_key)]
        assert "__cycle__" not in cb
        assert cb["attributes"]["id"] == "cb"
        assert cb["element_index"] == cb_key
        assert (cb["backend_node_id"], cb["session_id"], cb["target_id"]) == (10, "cross", "CROSS")
        assert cb["bounds"] == {"x": 50.0, "y": 650.0, "width": 30.0, "height": 30.0}
        main = entries["10"]
        assert (main["backend_node_id"], main["session_id"]) == (10, "tab")

    @pytest.mark.parametrize("element_id", ["cb", "nested-btn"])
    async def test_a_pointer_to_a_frame_element_names_that_element(self, element_id: str) -> None:
        """Backend id 10 names four different elements on this page."""
        state = await clean(reference_page())
        key, node = by_id(state, element_id)
        assert _node_to_json(node, {id(node)}, 0) == {
            "__cycle__": {
                "element_index": key,
                "backend_node_id": 10,
                "target_id": node.target_id,
            }
        }

    async def test_a_pointer_to_an_unindexed_frame_node_cannot_pass_for_a_page_key(self) -> None:
        """The frame document's ``<html>`` has backend id 1 — so does the page's."""
        state = await clean(reference_page())
        _, cb = by_id(state, "cb")
        html = cb.frame_owners()[0].content_document
        assert html is not None and html.element_index is None
        assert _node_to_json(html, {id(html)}, 0) == {
            "__cycle__": {"element_index": None, "backend_node_id": 1, "target_id": "CROSS"}
        }


class TestPerTargetLookups:
    """Snapshot, accessibility and listener data are joined by backend id.

    That join is only meaningful inside one target: backend id 30 below is a
    plain ``div`` in the page and a listener-bearing ``div`` in the frame, and
    the page's accessibility tree disables *its* 31.
    """

    @staticmethod
    def two_targets() -> TargetCapture:
        frame = compile_target(
            Doc(
                "FRAME",
                html_id=1,
                viewport=(300, 100),
                height=100,
                body=[
                    El("div", 30, bounds=(0, 0, 40, 20), attrs={"id": "frame-listener"}),
                    El("div", 31, bounds=(0, 30, 40, 20), attrs={"id": "frame-ax-button"}),
                ],
            ),
            session_id="frame",
            target_id="FRAME",
            namespace=1,
            js_click_listener_ids={30},
            ax_nodes=[ax_node(31, "button", focusable=True)],
        )
        return compile_target(
            Doc(
                "MAIN",
                html_id=1,
                body=[
                    El("div", 30, bounds=(0, 0, 200, 20), attrs={"id": "page-plain"}),
                    El("button", 31, bounds=(0, 30, 80, 20), attrs={"id": "page-disabled"}),
                    El("iframe", 32, bounds=(0, 60, 300, 100), oopif_frame_id="FRAME"),
                ],
            ),
            session_id="tab",
            frames={"FRAME": frame},
            ax_nodes=[ax_node(31, "button", disabled=True)],
        )

    async def test_listener_ids_apply_to_their_own_target_only(self) -> None:
        state = await clean(self.two_targets())
        ids = indexed_ids(state)
        assert "frame-listener" in ids
        assert "page-plain" not in ids

    async def test_accessibility_data_applies_to_its_own_target_only(self) -> None:
        state = await clean(self.two_targets())
        ids = indexed_ids(state)
        assert "frame-ax-button" in ids  # focusable per the frame's tree
        assert "page-disabled" not in ids  # disabled per the page's tree
        _, frame_button = by_id(state, "frame-ax-button")
        assert frame_button.ax_role == "button"


class TestPagesWithoutFrames:
    async def test_keys_are_backend_ids(self) -> None:
        capture = compile_target(
            Doc(
                "MAIN",
                html_id=1,
                body=[
                    El("a", 7, bounds=(0, 0, 50, 20), attrs={"id": "link", "href": "/x"}),
                    El("button", 8, bounds=(0, 30, 50, 20), attrs={"id": "b"}),
                ],
            ),
            session_id="tab",
        )
        state = await clean(capture)
        assert set(state.selector_map) == {7, 8}
        assert "[7]<a" in state.serialized_dom and "[8]<button" in state.serialized_dom
