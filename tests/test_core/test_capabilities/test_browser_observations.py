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
from dataclasses import dataclass, field
from typing import Any

from spectragent.core.capabilities.browser.dom.models import (
    TRACE_SKIP,
    DOMElementNode,
    DOMRect,
    DOMState,
    ScrollInfo,
)
from spectragent.core.capabilities.browser.observations import DOMObs


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

    # Index assignment walks ``children`` only, so iframe and shadow
    # nodes never reach the selector map — the tree is their only route
    # into the trace.
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
        leaf = _link(
            leaf, DOMElementNode(tag_name="div", backend_node_id=first_id + offset)
        )
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
        # A backend_node_id, not a bare null: the reader can look the cut
        # point up in the page the same way a ``__cycle__`` pointer works.
        assert isinstance(node["__truncated__"], int)


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

    def test_a_node_reached_twice_becomes_a_resolvable_pointer(self) -> None:
        state = build_page()
        button = state.selector_map[4]
        # Same node under two keys: the second is a pointer, and the
        # pointer carries the backend_node_id a reader can look up.
        state.selector_map[99] = button
        payload = fragment_json(state)
        emitted = [payload["selector_map"]["4"], payload["selector_map"]["99"]]
        stubs = [e for e in emitted if set(e) == {"__cycle__"}]
        assert len(stubs) == 1
        assert stubs[0]["__cycle__"] == 4

    def test_summary_counts_interactive_elements(self) -> None:
        state = build_page()
        assert DOMObs(selector_map=dict(state.selector_map)).summary() == (
            "1 interactive elements"
        )


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
