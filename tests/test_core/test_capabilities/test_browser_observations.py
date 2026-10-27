"""What a browser observation fragment puts on disk.

``DOMObs`` is the only fragment in the framework whose payload is a
cyclic object graph, so it is the only one that has to encode itself.
Nothing exercised that encoder before: every other test in the suite
passes ``element_tree=None`` or a plain string, so the code that runs
against a real page ran against nothing here.

These tests pin down the two things that were previously implicit — the
*policy* (which links get recorded) and the *mechanism* (the policy is
declared on the dataclass field, not kept as a list of names somewhere
else).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic_core import to_json

from spectragent.core.capabilities.browser.dom.models import (
    TRACE_SKIP,
    DOMElementNode,
    DOMRect,
    DOMState,
    EnhancedAXProperty,
    ScrollInfo,
)
from spectragent.core.capabilities.browser.observations import (
    _MAX_ELEMENT_DEPTH,
    DOMObs,
    _GraphDecoder,
    _to_json,
)
from spectragent.core.trace.reader import RunTrace
from tests._helpers.dom_payloads import clean, reference_page
from tests._helpers.fragment_samples import FRAME_BUTTON_HANDLE, dom_page
from tests._helpers.trace_builder import fragment, new_wal, step_record


def _link(parent: DOMElementNode, child: DOMElementNode) -> DOMElementNode:
    child.parent = parent
    parent.children.append(child)
    return child


def build_page() -> DOMState:
    """A small page with every structural feature the encoder must handle.

    ``html > body > [ div > button, iframe(#doc > a), host(#shadow > span) ]``
    with parent back-references wired everywhere, a compound control, and
    a ``DOMRect`` instance shared by two nodes.
    """
    shared_rect = DOMRect(x=1.0, y=2.0, width=3.0, height=4.0)

    root = DOMElementNode(tag_name="html", backend_node_id=1)
    body = _link(root, DOMElementNode(tag_name="body", backend_node_id=2))

    div = _link(body, DOMElementNode(tag_name="div", backend_node_id=3))
    button = _link(
        div,
        DOMElementNode(
            tag_name="button",
            backend_node_id=4,
            is_interactive=True,
            text_content="Buy",
            bounds=shared_rect,
        ),
    )
    button._compound_children.append({"role": "slider", "valuenow": 7})

    # Iframe: content lives under content_document, never under children.
    iframe = _link(body, DOMElementNode(tag_name="iframe", backend_node_id=5))
    iframe_doc = DOMElementNode(tag_name="#document", backend_node_id=6)
    iframe_doc.parent = iframe
    iframe.content_document = iframe_doc
    iframe_link = _link(
        iframe_doc,
        DOMElementNode(
            tag_name="a",
            backend_node_id=7,
            is_interactive=True,
            text_content="inside the frame",
            bounds=shared_rect,
        ),
    )

    # Shadow host: roots are deliberately kept out of ``children``.
    host = _link(body, DOMElementNode(tag_name="my-widget", backend_node_id=8))
    shadow = DOMElementNode(tag_name="#shadow-root", backend_node_id=9)
    shadow.parent = host
    host.shadow_roots = [shadow]
    shadow_span = _link(
        shadow,
        DOMElementNode(
            tag_name="span",
            backend_node_id=10,
            text_content="inside the shadow root",
        ),
    )

    # The selector map below deliberately lists only the button, so the
    # iframe and shadow nodes' only route into the trace is the tree —
    # which is what these tests pin. (Real index assignment indexes them
    # too; see ``test_browser_dom_cleaning_frames.py``.)
    assert iframe_link is not None and shadow_span is not None
    return DOMState(
        dom_tree=root,
        selector_map={4: button},
        scroll_info=ScrollInfo(scroll_height=2000.0, viewport_height=800.0),
        serialized_dom="[4]<button />",
    )


@dataclass
class _SelfReferential:
    """A shape the encoder has never been told about."""

    label: str = "outer"
    next: Any = None


def dump(obs: DOMObs) -> dict[str, Any]:
    return json.loads(obs.model_dump_json())


def fragment_json(state: DOMState) -> dict[str, Any]:
    return dump(DOMObs(element_tree=state, selector_map=dict(state.selector_map)))


class TestRealDOMSurvivesTheWAL:
    """A page with back-references has to reach disk at all."""

    def test_cyclic_graph_round_trips(self) -> None:
        # Pydantic's stock dataclass encoder raises "Circular reference
        # detected" here; the fragment's own encoder must not.
        payload = fragment_json(build_page())
        assert payload["element_tree"]["dom_tree"]["tag_name"] == "html"

    def test_back_reference_is_never_recorded(self) -> None:
        raw = DOMObs(element_tree=build_page()).model_dump_json()
        assert '"parent"' not in raw

    def test_entering_at_a_leaf_does_not_drag_the_document_back_down(self) -> None:
        # ``selector_map`` entries are deep nodes. Following ``parent``
        # from one of them re-emits the entire page under every key.
        state = build_page()
        entry = json.dumps(fragment_json(state)["selector_map"]["4"])
        assert "button" in entry
        assert "html" not in entry
        assert "body" not in entry

    def test_scalar_and_value_objects_survive(self) -> None:
        tree = fragment_json(build_page())["element_tree"]
        assert tree["scroll_info"]["scroll_height"] == 2000.0
        button = tree["dom_tree"]["children"][0]["children"][0]["children"][0]
        assert button["bounds"] == {"x": 1.0, "y": 2.0, "width": 3.0, "height": 4.0}

    def test_an_unexpected_self_referential_shape_terminates(self) -> None:
        # The previous encoder walked any dataclass it did not recognise
        # with no guard, so one unexpected shape was a RecursionError in
        # the middle of a WAL write.
        loop = _SelfReferential()
        loop.next = loop
        payload = dump(DOMObs(element_tree=loop))["element_tree"]
        assert payload["label"] == "outer"
        assert payload["next"] == {"__cycle__": None}

    def test_a_shared_value_object_is_not_mistaken_for_a_cycle(self) -> None:
        # One DOMRect instance hangs off two different nodes. Only nodes
        # are tracked for repeats, so both copies must be spelled out.
        tree = fragment_json(build_page())["element_tree"]["dom_tree"]
        rect = {"x": 1.0, "y": 2.0, "width": 3.0, "height": 4.0}
        body = tree["children"][0]
        button = body["children"][0]["children"][0]
        iframe_link = body["children"][1]["content_document"]["children"][0]
        assert button["bounds"] == rect
        assert iframe_link["bounds"] == rect


def _chain(depth: int, first_id: int) -> tuple[DOMElementNode, DOMElementNode]:
    """A single-child chain ``depth`` elements long; returns (root, leaf)."""
    root = DOMElementNode(tag_name="div", backend_node_id=first_id)
    leaf = root
    for offset in range(1, depth):
        leaf = _link(leaf, DOMElementNode(tag_name="div", backend_node_id=first_id + offset))
    return root, leaf


class TestDepthCannotKillTheRun:
    """A page too deep must cost a subtree, never the run.

    ``model_dump_json`` is a pydantic-core call and pydantic-core gives up
    past ~256 levels of output nesting, reporting it as "Circular
    reference detected". Since ``TraceWriter`` is a blocking hook on
    ``record_step``, that exception propagates into the loop and ends the
    run at step 0 with an empty trace. The encoder emits ~2 levels per DOM
    level and an iframe's nesting adds to its host's, so two 70-deep trees
    — one inside the other — used to be enough.
    """

    def test_an_iframe_deeper_than_the_serializer_still_reaches_disk(self) -> None:
        root, leaf = _chain(70, first_id=1)
        doc, _ = _chain(70, first_id=10_000)
        doc.parent = leaf
        leaf.content_document = doc
        state = DOMState(dom_tree=root, selector_map={}, serialized_dom="[1]<div />")

        raw = DOMObs(element_tree=state).model_dump_json()

        assert '"__truncated__"' in raw, "nothing was cut, so nothing was budgeted"
        assert json.loads(raw)["element_tree"]["dom_tree"]["tag_name"] == "div"

    def test_a_shadow_root_deeper_than_the_serializer_does_the_same(self) -> None:
        root, leaf = _chain(70, first_id=1)
        shadow, _ = _chain(70, first_id=20_000)
        shadow.parent = leaf
        leaf.shadow_roots = [shadow]
        state = DOMState(dom_tree=root, selector_map={}, serialized_dom="[1]<div />")

        assert '"__truncated__"' in DOMObs(element_tree=state).model_dump_json()

    def test_a_page_deeper_than_any_real_one_is_untouched(self) -> None:
        """40 combined levels is the deepest measured on a live page."""
        root, leaf = _chain(40, first_id=1)
        doc, _ = _chain(40, first_id=10_000)
        doc.parent = leaf
        leaf.content_document = doc
        state = DOMState(dom_tree=root, selector_map={}, serialized_dom="[1]<div />")

        raw = DOMObs(element_tree=state).model_dump_json()
        assert "__truncated__" not in raw

    def test_the_marker_says_where_the_trace_stopped(self) -> None:
        root, _leaf = _chain(300, first_id=1)
        state = DOMState(dom_tree=root, selector_map={}, serialized_dom="[1]<div />")

        payload = json.loads(DOMObs(element_tree=state).model_dump_json())
        node = payload["element_tree"]["dom_tree"]
        while "__truncated__" not in node:
            node = node["children"][0]
        # The node's identity, not a bare null: the reader can look the cut
        # point up in the page the same way a ``__cycle__`` pointer works.
        assert node["__truncated__"] == {
            "element_index": None,
            "backend_node_id": 1 + _MAX_ELEMENT_DEPTH // 2,
            "target_id": None,
        }


def _composed_depth(node: DOMElementNode | None) -> int:
    """Length of a single-child chain, counting frame documents and shadow roots."""
    depth = 0
    while node is not None:
        depth += 1
        below = node.composed_children()
        node = below[0] if below else None
    return depth


class TestADeepPageReadsBackThroughTheTrace:
    """The depth budget is a promise about the *trace*, so hold it through ``RunTrace``.

    pydantic's JSON parser refuses nesting past 200 levels; the encoder's
    budget allows ~204 and pydantic-core's serializer emits up to ~254. So
    every page the encoder truncated — and every one a little under the
    budget — committed without error and then raised ``TraceSchemaError``
    ("Invalid JSON: recursion limit exceeded") on every read, losing the
    whole fragment rather than the cut subtree. The tests above parse with
    ``json.loads`` and could not see it.
    """

    @staticmethod
    def _round_trip(tmp_path: Path, state: DOMState) -> DOMState:
        wal = new_wal(tmp_path)
        page = DOMObs(element_tree=state, selector_map=dict(state.selector_map))
        wal.commit_step(0, step_record(0), fragments=[fragment(0, page)])

        obs = RunTrace(wal.run_dir).observation_fragment(0, "browser.dom")

        assert isinstance(obs, DOMObs)
        assert isinstance(obs.element_tree, DOMState)
        return obs.element_tree

    def test_an_untruncated_chain_past_the_parsers_limit(self, tmp_path: Path) -> None:
        """100 nested elements: no truncation, but 202 levels of JSON — tree and entry alike."""
        root, _leaf = _chain(100, first_id=1)
        state = DOMState(dom_tree=root, selector_map={1: root}, serialized_dom="[1]<div />")

        tree = self._round_trip(tmp_path, state)

        assert _composed_depth(tree.dom_tree) == 100
        assert _composed_depth(tree.selector_map[1]) == 100

    @pytest.mark.parametrize("host", ["iframe", "shadow_root"])
    def test_a_page_the_encoder_truncated(self, tmp_path: Path, host: str) -> None:
        """The pages ``TestDepthCannotKillTheRun`` writes are the pages a reader must get back."""
        root, leaf = _chain(70, first_id=1)
        inner, _ = _chain(70, first_id=10_000)
        inner.parent = leaf
        if host == "iframe":
            leaf.content_document = inner
        else:
            leaf.shadow_roots = [inner]
        state = DOMState(dom_tree=root, selector_map={}, serialized_dom="[1]<div />")
        assert "__truncated__" in DOMObs(element_tree=state).model_dump_json()

        tree = self._round_trip(tmp_path, state)

        # Everything the encoder wrote, and nothing past the cut.
        assert 70 < _composed_depth(tree.dom_tree) < 140


class TestWhatTheTraceContains:
    """The recorded page is the page the agent read."""

    def test_iframe_content_is_recorded(self) -> None:
        # The text serializer walks content_document into the prompt, so
        # the agent saw this. A trace that drops it cannot explain the run.
        raw = json.dumps(fragment_json(build_page())["element_tree"])
        assert "inside the frame" in raw

    def test_shadow_root_content_is_recorded(self) -> None:
        raw = json.dumps(fragment_json(build_page())["element_tree"])
        assert "inside the shadow root" in raw

    def test_private_fields_are_not_recorded(self) -> None:
        raw = DOMObs(element_tree=build_page()).model_dump_json()
        assert "_compound_children" not in raw
        assert "slider" not in raw

    def test_selector_map_is_recorded_exactly_once(self) -> None:
        payload = fragment_json(build_page())
        # Nested inside DOMState it produced nothing but dead
        # ``{"__cycle__": N}`` stubs, because dom_tree got there first.
        assert "selector_map" not in payload["element_tree"]
        # The fragment's own field is the real copy.
        assert set(payload["selector_map"]) == {"4"}
        assert payload["selector_map"]["4"]["tag_name"] == "button"

    def test_a_node_under_two_keys_is_written_in_full_under_both(self) -> None:
        """No entry is a pointer into another — each is readable on its own."""
        state = build_page()
        state.selector_map[99] = state.selector_map[4]
        payload = fragment_json(state)
        assert payload["selector_map"]["99"] == payload["selector_map"]["4"]
        assert payload["selector_map"]["99"]["bounds"] == {
            "x": 1.0,
            "y": 2.0,
            "width": 3.0,
            "height": 4.0,
        }

    def test_a_node_reached_twice_inside_one_encoding_is_still_a_pointer(self) -> None:
        """Within the tree the visited set still stops a repeat — and names it."""
        state = build_page()
        assert state.dom_tree is not None
        body = state.dom_tree.children[0]
        div = body.children[0]
        body.children.append(div)

        repeat = fragment_json(state)["element_tree"]["dom_tree"]["children"][0]["children"][-1]

        assert repeat == {
            "__cycle__": {"element_index": None, "backend_node_id": 3, "target_id": None}
        }

    def test_summary_counts_interactive_elements(self) -> None:
        state = build_page()
        assert DOMObs(selector_map=dict(state.selector_map)).summary() == ("1 interactive elements")


def _subtree_text(node: DOMElementNode) -> list[str]:
    """Every piece of text under *node*, in document order."""
    out: list[str] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.text_content:
            out.append(current.text_content)
        if current.node_value:
            out.append(current.node_value)
        stack.extend(reversed(current.composed_children()))
    return out


class TestSelectorMapEntriesAreSelfContained:
    """Regression: the entries shared one visited set.

    An indexed element nested under an earlier-indexed one — anything in an
    indexed iframe, or in an indexed form — was written as a
    ``{"__cycle__": ref}`` stub of its own entry, with no bounds: 59 of 60
    entries on one M18 BrowserUse trace. Retargeting an element and turning
    an index into a coordinate both read ``selector_map[index].bounds``.
    """

    async def test_every_entry_of_a_framed_page_is_written_in_full(self) -> None:
        state = await clean(reference_page())
        nested = [key for key, node in state.selector_map.items() if node.frame_owners()]
        assert nested, "the fixture must index elements inside frames"

        entries = fragment_json(state)["selector_map"]

        for key, node in state.selector_map.items():
            entry = entries[str(key)]
            assert "__cycle__" not in entry, f"entry {key} is a stub"
            assert entry["tag_name"] == node.tag_name
            assert entry["element_index"] == key
            assert node.bounds is not None
            assert entry["bounds"] == {
                "x": node.bounds.x,
                "y": node.bounds.y,
                "width": node.bounds.width,
                "height": node.bounds.height,
            }

    async def test_every_entry_reads_back_with_bounds_tag_attributes_and_text(self) -> None:
        state = await clean(reference_page())
        payload = fragment_json(state)

        decoded = DOMObs.from_trace(payload, None)

        assert set(decoded.selector_map) == set(state.selector_map)
        for key, node in state.selector_map.items():
            entry = decoded.selector_map[key]
            assert isinstance(entry, DOMElementNode)
            assert entry.element_index == key
            assert entry.tag_name == node.tag_name
            assert entry.attributes == node.attributes
            assert entry.bounds == node.bounds
            assert (entry.session_id, entry.target_id) == (node.session_id, node.target_id)
            assert _subtree_text(entry) == _subtree_text(node)


class TestReadingTheTraceBack:
    """``DOMObs.from_trace``: the encoding's inverse, typed, and honest about losses."""

    @staticmethod
    def _read(state: DOMState) -> DOMObs:
        return DOMObs.from_trace(fragment_json(state), None)

    def test_the_tree_comes_back_as_a_dom_state(self) -> None:
        obs = self._read(build_page())

        tree = obs.element_tree
        assert isinstance(tree, DOMState)
        assert tree.serialized_dom == "[4]<button />"
        assert tree.scroll_info == ScrollInfo(scroll_height=2000.0, viewport_height=800.0)
        assert tree.dom_tree is not None and tree.dom_tree.tag_name == "html"
        button = tree.dom_tree.children[0].children[0].children[0]
        assert button.bounds == DOMRect(x=1.0, y=2.0, width=3.0, height=4.0)
        assert button.text_content == "Buy"

    def test_the_state_and_the_fragment_share_one_selector_map(self) -> None:
        obs = self._read(build_page())
        assert isinstance(obs.element_tree, DOMState)
        assert obs.element_tree.selector_map is obs.selector_map

    def test_every_traced_field_type_comes_back_typed(self) -> None:
        obs = self._read(dom_page())

        button = obs.selector_map[4]
        assert button.ax_properties == [
            EnhancedAXProperty(name="focusable", value=True),
            EnhancedAXProperty(name="level", value=2),
            EnhancedAXProperty(name="valuetext", value="pay"),
            EnhancedAXProperty(name="description", value=None),
        ]
        assert button.computed_styles == {"display": "inline-block", "cursor": "pointer"}
        form = obs.selector_map[3]
        assert form.client_rect == DOMRect(x=0.0, y=0.0, width=400.0, height=200.0)
        assert form.scroll_rects == DOMRect(x=0.0, y=0.0, width=400.0, height=600.0)
        # An entry carries its whole subtree: the form's holds the button.
        assert form.children[0].tag_name == "button"
        frame_button = obs.selector_map[FRAME_BUTTON_HANDLE]
        assert (frame_button.target_id, frame_button.text_content) == ("CARD", "Confirm card")

    def test_parent_links_are_rebuilt_inside_the_tree(self) -> None:
        obs = self._read(build_page())
        assert isinstance(obs.element_tree, DOMState) and obs.element_tree.dom_tree is not None
        html = obs.element_tree.dom_tree
        body = html.children[0]
        iframe = body.children[1]
        assert iframe.content_document is not None
        link = iframe.content_document.children[0]
        shadow_roots = body.children[2].shadow_roots
        assert shadow_roots is not None
        shadow_span = shadow_roots[0].children[0]

        assert html.parent is None
        assert link.frame_owners() == [iframe]
        assert shadow_span.parent is not None and shadow_span.parent.tag_name == "#shadow-root"

    def test_an_entry_has_no_parent_above_itself(self) -> None:
        """What sits above an entry is not in the entry — so ``parent`` stays unset."""
        obs = self._read(dom_page())
        frame_button = obs.selector_map[FRAME_BUTTON_HANDLE]
        assert frame_button.parent is None
        # Inside an entry, the links are there.
        iframe = obs.selector_map[8]
        assert iframe.content_document is not None
        assert iframe.content_document.children[0].frame_owners() == [iframe]

    def test_a_truncated_subtree_is_not_invented(self) -> None:
        root, _leaf = _chain(300, first_id=1)
        obs = self._read(DOMState(dom_tree=root, serialized_dom="[1]<div />"))

        assert isinstance(obs.element_tree, DOMState) and obs.element_tree.dom_tree is not None
        node = obs.element_tree.dom_tree
        while node.children:
            node = node.children[0]
        # The chain ends at the last node the trace wrote; the marker that
        # named the next one is dropped, not turned into an empty node.
        assert node.backend_node_id == _MAX_ELEMENT_DEPTH // 2

    def test_a_pointer_inside_the_tree_is_dropped_not_followed(self) -> None:
        state = build_page()
        assert state.dom_tree is not None
        body = state.dom_tree.children[0]
        body.children.append(body.children[0])

        obs = self._read(state)

        assert isinstance(obs.element_tree, DOMState) and obs.element_tree.dom_tree is not None
        tags = [child.tag_name for child in obs.element_tree.dom_tree.children[0].children]
        assert tags == ["div", "iframe", "my-widget"]

    def test_scalars_come_back_as_they_were_written(self) -> None:
        payload = json.loads(DOMObs(element_tree="TREE", selector_map={1: "x"}).model_dump_json())
        obs = DOMObs.from_trace(payload, None)
        assert obs.element_tree == "TREE"
        assert obs.selector_map == {1: "x"}

    def test_a_stub_entry_is_refused(self) -> None:
        payload = fragment_json(build_page())
        payload["selector_map"]["4"] = {
            "__cycle__": {"element_index": 4, "backend_node_id": 4, "target_id": None}
        }
        with pytest.raises(ValueError, match="marker"):
            DOMObs.from_trace(payload, None)

    def test_a_field_the_node_does_not_have_is_a_schema_mismatch(self) -> None:
        payload = fragment_json(build_page())
        payload["selector_map"]["4"]["renamed_field"] = 1
        with pytest.raises(ValueError, match="renamed_field"):
            DOMObs.from_trace(payload, None)

    def test_a_value_of_the_wrong_type_is_a_schema_mismatch(self) -> None:
        payload = fragment_json(build_page())
        payload["selector_map"]["4"]["bounds"]["x"] = "left"
        with pytest.raises(ValueError, match="left"):
            DOMObs.from_trace(payload, None)

    def test_a_node_without_its_tag_is_a_schema_mismatch(self) -> None:
        payload = fragment_json(build_page())
        del payload["element_tree"]["dom_tree"]["tag_name"]
        with pytest.raises(ValueError, match="DOMElementNode"):
            DOMObs.from_trace(payload, None)

    def test_an_artifact_is_refused(self) -> None:
        with pytest.raises(ValueError, match="artifact"):
            DOMObs.from_trace(fragment_json(build_page()), b"\x89PNG")


# ── the mechanism, not the names ───────────────────────────────────
#
# These use throwaway dataclasses on purpose. The encoder must read the
# policy off the field it is looking at; if it instead matched field
# names it had been told about, these would all go the wrong way.


@dataclass
class _Marked:
    # ``tag_name`` used to be the "this is a DOM node" dispatch, so this
    # fixture lands on the same code path a real node does — and must
    # still be judged field by field.
    tag_name: str = "widget"
    kept: str = "kept"
    # Named exactly like the DOM's back-reference, but unmarked.
    parent: str = "not-a-back-reference"
    # Marked, under a name the framework has never heard of.
    huge_side_channel: str = field(default="dropped", metadata=TRACE_SKIP)
    # Marked, under the DOM's own name for it — the same decision,
    # carried by the field rather than by the string.
    content_document: str = field(default="dropped", metadata=TRACE_SKIP)
    # Private: dropped by the general rule, with no marker at all.
    _internal: str = "dropped"


class TestPolicyRidesOnTheField:
    def test_a_marked_field_is_dropped_whatever_it_is_called(self) -> None:
        tree = dump(DOMObs(element_tree=_Marked()))["element_tree"]
        assert "huge_side_channel" not in tree
        assert "content_document" not in tree

    def test_an_unmarked_field_is_kept_even_if_it_is_called_parent(self) -> None:
        tree = dump(DOMObs(element_tree=_Marked()))["element_tree"]
        assert tree["parent"] == "not-a-back-reference"

    def test_private_fields_need_no_marker(self) -> None:
        tree = dump(DOMObs(element_tree=_Marked()))["element_tree"]
        assert "_internal" not in tree

    def test_ordinary_fields_are_kept(self) -> None:
        tree = dump(DOMObs(element_tree=_Marked()))["element_tree"]
        assert tree["kept"] == "kept"

    def test_renaming_a_dom_field_carries_its_decision_along(self) -> None:
        # The failure this replaces: renaming ``parent`` upstream left a
        # stale string behind, and the encoder silently started writing
        # the whole document under every selector_map entry.
        renamed = DOMElementNode.__dataclass_fields__["parent"]
        assert renamed.metadata.get("trace") == "skip"


# ── the decoder's plans: every annotation shape terminates ─────────


@dataclass
class _Shapes:
    """Annotations a future traced field may plausibly carry."""

    # CDP's shadow-root type is exactly this closed set.
    shadow_root_type: Literal["open", "closed", "user-agent"] | None = None
    point: tuple[float, float] = (0.0, 0.0)
    path: tuple[int, ...] = ()
    tags: frozenset[str] = frozenset()
    kinds: list[Literal["a", "b"]] = field(default_factory=list)


class TestTheDecoderKnowsEveryShapeItAccepts:
    """``_GraphDecoder`` builds a plan per annotation, and building one always ends.

    ``_field`` used to hand any generic it had no branch for back to
    ``_item``, which handed it straight back — so the day a traced field
    was annotated ``Literal[...]``, ``tuple[...]`` or ``set[...]``, every
    ``DOMObs`` read became a bare ``RecursionError``, not even a
    ``ValueError`` the reader could name a file for.
    """

    @pytest.mark.parametrize(
        ("annotation", "recorded", "decoded"),
        [
            (Literal["open", "closed", "user-agent"], "closed", "closed"),
            (Literal["open", "closed"] | None, None, None),
            (Literal["open", "closed"] | None, "open", "open"),
            (tuple[float, float], [1, 2.5], (1, 2.5)),
            (tuple[int, ...], [1, 2, 3], (1, 2, 3)),
            (set[str], ["a", "b"], {"a", "b"}),
            (frozenset[str], ["a"], frozenset({"a"})),
            (list[Literal["a", "b"]], ["b", "a"], ["b", "a"]),
            (dict[str, tuple[int, int]], {"k": [1, 2]}, {"k": (1, 2)}),
            (tuple, [1, "x"], (1, "x")),
        ],
    )
    def test_a_known_shape_decodes_to_its_own_type(
        self, annotation: Any, recorded: Any, decoded: Any
    ) -> None:
        value = _GraphDecoder()._field(annotation)(recorded)
        assert value == decoded
        assert type(value) is type(decoded)

    @pytest.mark.parametrize(
        ("annotation", "recorded"),
        [
            (Literal["open", "closed"], "bogus"),
            (Literal[1], True),  # True == 1 in Python; not in a trace
            (Literal[True], 1),
            (tuple[float, float], [1.0]),  # wrong arity
            (set[str], "a"),
        ],
    )
    def test_a_value_outside_the_shape_is_a_schema_mismatch(
        self, annotation: Any, recorded: Any
    ) -> None:
        with pytest.raises(ValueError):
            _GraphDecoder()._field(annotation)(recorded)

    @pytest.mark.parametrize(
        "annotation",
        [Sequence[str], Iterable[int], Mapping[str, int], Literal["a"] | int],
        ids=["Sequence", "Iterable", "Mapping", "union-with-a-literal"],
    )
    def test_a_shape_without_a_rule_is_refused_when_the_plan_is_built(
        self, annotation: Any
    ) -> None:
        """A programming error, named — not a stack overflow on the first read."""
        with pytest.raises(TypeError, match="decod"):
            _GraphDecoder()._field(annotation)

    def test_a_dataclass_of_those_shapes_round_trips(self) -> None:
        original = _Shapes(
            shadow_root_type="closed",
            point=(1.5, 2.0),
            path=(3, 4),
            tags=frozenset({"x"}),
            kinds=["b"],
        )
        recorded = json.loads(to_json(_to_json(original, set())))

        assert _GraphDecoder().dataclass(_Shapes, recorded) == original
