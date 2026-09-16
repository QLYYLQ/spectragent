"""Synthetic CDP payloads for pages with frames and shadow roots.

``DOM.getDocument`` and ``DOMSnapshot.captureSnapshot`` describe the same
nodes twice, joined only by ``backendNodeId``. Writing both by hand for a page
with three documents is where a test would silently disagree with itself, so
one element tree is written once and compiled into both — with the shapes a
real Chrome 152 produced for the fixture pages in
``test_core/test_capabilities/test_browser_frames_live.py``:

* an ``<html>`` element carries its document's ``frameId``;
* an ``<iframe>`` carries the *child* frame's ``frameId``, and a
  ``contentDocument`` only when the frame runs in the same target;
* snapshot ``bounds`` are relative to the element's own document, one
  snapshot document per same-target frame, each with its scroll offset.

:func:`reference_page` is the multi-frame page the cleaning and action tests
share, so the action tests act on nodes with real parent links, sessions and
namespaced keys.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from spectragent.core.capabilities.browser.dom.cleaning.browser_use import (
    REQUIRED_COMPUTED_STYLES,
    BrowserUseCleaner,
)
from spectragent.core.capabilities.browser.dom.models import (
    DOMElementNode,
    DOMState,
    TargetCapture,
)
from spectragent.core.capabilities.browser.dom.serializers.text_serializer import TextSerializer

_DEFAULT_STYLES: dict[str, str] = {
    "display": "block",
    "visibility": "visible",
    "opacity": "1",
    "overflow": "visible",
    "overflow-x": "visible",
    "overflow-y": "visible",
    "cursor": "auto",
    "pointer-events": "auto",
    "position": "static",
    "background-color": "rgba(0, 0, 0, 0)",
    "border-left-width": "0px",
    "border-top-width": "0px",
    "padding-left": "0px",
    "padding-top": "0px",
}


@dataclass
class El:
    """One element (or text node, ``tag="#text"``) of a synthetic page."""

    tag: str
    backend_id: int
    bounds: tuple[float, float, float, float] | None = None
    attrs: dict[str, str] = field(default_factory=dict)
    styles: dict[str, str] = field(default_factory=dict)
    children: list[El] = field(default_factory=list)
    text: str = ""
    client: tuple[float, float, float, float] | None = None
    scroll: tuple[float, float, float, float] | None = None
    paint_order: int = 1
    #: Open shadow root content (the root itself gets ``shadow_root_id``).
    shadow: list[El] | None = None
    shadow_root_id: int = 0
    #: Same-target frame document.
    content: Doc | None = None
    #: Frame id of an out-of-process child frame (no ``content``).
    oopif_frame_id: str | None = None


@dataclass
class Doc:
    """One document: its frame id, scroll offset, viewport and ``<html>`` subtree."""

    frame_id: str
    html_id: int
    body: list[El]
    viewport: tuple[float, float] = (1280.0, 720.0)
    scroll: tuple[float, float] = (0.0, 0.0)
    height: float = 3000.0
    document_id: int = 0
    body_attrs: dict[str, str] = field(default_factory=dict)


class _Compiler:
    def __init__(self) -> None:
        self.strings: list[str] = []
        self.string_index: dict[str, int] = {}
        self.documents: list[dict[str, Any]] = []
        self.next_node_id = 1

    def s(self, value: str) -> int:
        if value not in self.string_index:
            self.string_index[value] = len(self.strings)
            self.strings.append(value)
        return self.string_index[value]

    def node_id(self) -> int:
        nid = self.next_node_id
        self.next_node_id += 1
        return nid

    def compile_doc(self, doc: Doc) -> dict[str, Any]:
        layout: dict[str, list[Any]] = {
            "nodeIndex": [],
            "bounds": [],
            "styles": [],
            "clientRects": [],
            "scrollRects": [],
            "paintOrders": [],
        }
        snap_doc: dict[str, Any] = {
            "frameId": self.s(doc.frame_id),
            "scrollOffsetX": doc.scroll[0],
            "scrollOffsetY": doc.scroll[1],
            "nodes": {"backendNodeId": []},
            "layout": layout,
        }
        self.documents.append(snap_doc)

        html = El(
            "html",
            doc.html_id,
            bounds=(0.0, 0.0, doc.viewport[0], doc.height),
            client=(0.0, 0.0, doc.viewport[0], doc.viewport[1]),
            scroll=(doc.scroll[0], doc.scroll[1], doc.viewport[0], doc.height),
            children=[
                El(
                    "body",
                    doc.html_id + 1,
                    bounds=(0.0, 0.0, doc.viewport[0], doc.height),
                    attrs=dict(doc.body_attrs),
                    children=doc.body,
                )
            ],
        )
        html_node = self.compile_el(html, snap_doc)
        html_node["frameId"] = doc.frame_id
        return {
            "nodeId": self.node_id(),
            "backendNodeId": doc.document_id or doc.html_id + 1000,
            "nodeType": 9,
            "nodeName": "#document",
            "children": [html_node],
        }

    def layout_entry(self, el: El, snap_doc: dict[str, Any]) -> None:
        backend_ids: list[int] = snap_doc["nodes"]["backendNodeId"]
        index = len(backend_ids)
        backend_ids.append(el.backend_id)
        if el.bounds is None:
            return
        layout = snap_doc["layout"]
        styles = {**_DEFAULT_STYLES, **el.styles}
        width, height = el.bounds[2], el.bounds[3]
        layout["nodeIndex"].append(index)
        layout["bounds"].append(list(el.bounds))
        layout["styles"].append([self.s(styles[name]) for name in REQUIRED_COMPUTED_STYLES])
        layout["clientRects"].append(list(el.client or (0.0, 0.0, width, height)))
        layout["scrollRects"].append(list(el.scroll or (0.0, 0.0, width, height)))
        layout["paintOrders"].append(el.paint_order)

    def compile_el(self, el: El, snap_doc: dict[str, Any]) -> dict[str, Any]:
        self.layout_entry(el, snap_doc)
        if el.tag == "#text":
            return {
                "nodeId": self.node_id(),
                "backendNodeId": el.backend_id,
                "nodeType": 3,
                "nodeName": "#text",
                "nodeValue": el.text,
            }
        attributes: list[str] = []
        for key, value in el.attrs.items():
            attributes.extend([key, value])
        node: dict[str, Any] = {
            "nodeId": self.node_id(),
            "backendNodeId": el.backend_id,
            "nodeType": 1,
            "nodeName": el.tag.upper(),
            "attributes": attributes,
            "children": [self.compile_el(child, snap_doc) for child in el.children],
        }
        if el.shadow is not None:
            root = {
                "nodeId": self.node_id(),
                "backendNodeId": el.shadow_root_id,
                "nodeType": 11,
                "nodeName": "#document-fragment",
                "shadowRootType": "open",
                "children": [self.compile_el(child, snap_doc) for child in el.shadow],
            }
            node["shadowRoots"] = [root]
        if el.content is not None:
            node["frameId"] = el.content.frame_id
            node["contentDocument"] = self.compile_doc(el.content)
        elif el.oopif_frame_id is not None:
            node["frameId"] = el.oopif_frame_id
        return node


def compile_target(
    doc: Doc,
    *,
    session_id: str,
    target_id: str | None = None,
    namespace: int = 0,
    frames: dict[str, TargetCapture] | None = None,
    ax_nodes: list[dict[str, Any]] | None = None,
    js_click_listener_ids: set[int] | None = None,
) -> TargetCapture:
    """One target's ``TargetCapture``: its documents as the CDP payloads carry them."""
    compiler = _Compiler()
    root = compiler.compile_doc(doc)
    return TargetCapture(
        raw_dom={"root": root},
        snapshot={"strings": compiler.strings, "documents": compiler.documents},
        ax_tree={"nodes": ax_nodes or []},
        session_id=session_id,
        target_id=target_id,
        namespace=namespace,
        js_click_listener_ids=js_click_listener_ids,
        frames=frames or {},
    )


def ax_node(backend_id: int, role: str, **properties: Any) -> dict[str, Any]:
    """An ``Accessibility.getFullAXTree`` node."""
    return {
        "backendDOMNodeId": backend_id,
        "role": {"value": role},
        "properties": [{"name": k, "value": {"value": v}} for k, v in properties.items()],
    }


def iframe_styles(border: float, padding: float, **extra: str) -> dict[str, str]:
    return {
        "display": "inline",
        "border-left-width": f"{border}px",
        "border-top-width": f"{border}px",
        "padding-left": f"{padding}px",
        "padding-top": f"{padding}px",
        **extra,
    }


# ── the reference page ──────────────────────────────────────────────

#: Namespaces of the reference page's frame targets, in attach order.
CROSS = 1
NESTED = 2
TINY = 3


def reference_page(*, overlay: bool = False) -> TargetCapture:
    """One of everything, with geometry that makes every page-space term visible::

        main document (scrolled 100 down)
        ├── button#main-btn                        backend 10
        ├── iframe#same   324x174, border 5, padding 7   same target, scrolled 40
        │   └── button#same-btn                    backend 52
        ├── div#host ── shadow ── button#shadow-btn backend 14
        ├── iframe#cross  340x220, border 4, padding 6   out of process, scrolled 10
        │   ├── div#cb role=checkbox               backend 10  (same id as main-btn)
        │   ├── input#cross-input                  backend 11
        │   └── iframe#nested 210x90, border 3, padding 2  out of process again
        │       └── button#nested-btn              backend 10  (again)
        ├── iframe#tiny   304x78                   out of process
        │   └── div#anchor role=checkbox           backend 10
        └── iframe#opaque 304x78                   out of process, never captured

    ``overlay`` adds an opaque modal painted over the whole cross-origin frame.
    """
    nested = compile_target(
        Doc(
            "NESTED",
            html_id=1,
            viewport=(200, 80),
            height=80,
            body=[
                El("button", 10, bounds=(5, 7, 60, 21), attrs={"id": "nested-btn"}),
            ],
        ),
        session_id="nested",
        target_id="NESTED",
        namespace=NESTED,
    )
    cross = compile_target(
        Doc(
            "CROSS",
            html_id=1,
            viewport=(320, 200),
            scroll=(0, 10),
            height=800,
            body=[
                El("div", 10, bounds=(0, 50, 30, 30), attrs={"id": "cb", "role": "checkbox"}),
                El("input", 11, bounds=(0, 82, 150, 21), attrs={"id": "cross-input"}),
                El(
                    "iframe",
                    12,
                    bounds=(0, 120, 210, 90),
                    attrs={"id": "nested"},
                    styles=iframe_styles(3, 2),
                    oopif_frame_id="NESTED",
                ),
            ],
        ),
        session_id="cross",
        target_id="CROSS",
        namespace=CROSS,
        frames={"NESTED": nested},
    )
    tiny = compile_target(
        Doc(
            "TINY",
            html_id=1,
            viewport=(304, 78),
            height=78,
            body=[
                El(
                    "div",
                    10,
                    bounds=(1, 1, 28, 28),
                    attrs={"id": "anchor", "role": "checkbox", "tabindex": "0"},
                ),
            ],
        ),
        session_id="tiny",
        target_id="TINY",
        namespace=TINY,
    )
    body = [
        El("button", 10, bounds=(0, 200, 80, 20), attrs={"id": "main-btn"}),
        El(
            "iframe",
            11,
            bounds=(100, 300, 324, 174),
            attrs={"id": "same"},
            styles=iframe_styles(5, 7),
            content=Doc(
                "SAME",
                html_id=50,
                viewport=(300, 150),
                scroll=(0, 40),
                height=700,
                body=[
                    El("button", 52, bounds=(0, 102, 50, 21), attrs={"id": "same-btn"}),
                    # Past the frame's right edge: rendered nowhere.
                    El("button", 53, bounds=(400, 102, 50, 21), attrs={"id": "same-clipped"}),
                ],
            ),
        ),
        El(
            "div",
            12,
            bounds=(0, 500, 1280, 23),
            attrs={"id": "host"},
            shadow=[El("button", 14, bounds=(0, 502, 60, 21), attrs={"id": "shadow-btn"})],
            shadow_root_id=13,
        ),
        El(
            "iframe",
            15,
            bounds=(40, 600, 340, 220),
            attrs={"id": "cross"},
            styles=iframe_styles(4, 6),
            oopif_frame_id="CROSS",
            paint_order=2,
        ),
        El("iframe", 16, bounds=(40, 850, 304, 78), attrs={"id": "tiny"}, oopif_frame_id="TINY"),
        El("iframe", 17, bounds=(400, 850, 304, 78), attrs={"id": "opaque"}, oopif_frame_id="GONE"),
    ]
    if overlay:
        body.append(
            El(
                "div",
                18,
                bounds=(0, 580, 800, 300),
                attrs={"id": "modal"},
                styles={"background-color": "rgb(255, 255, 255)"},
                paint_order=9,
            )
        )
    return compile_target(
        Doc("MAIN", html_id=1, scroll=(0, 100), body=body),
        session_id="tab",
        frames={"CROSS": cross, "TINY": tiny},
    )


async def clean(
    capture: TargetCapture,
    previous: dict[int, DOMElementNode] | None = None,
    **cleaner_options: Any,
) -> DOMState:
    """Run the ``browser_use`` cleaner and the text serializer, as the DOM resolver does."""
    state = await BrowserUseCleaner(**cleaner_options).clean(
        capture, (1280, 720), previous_selector_map=previous
    )
    state.serialized_dom = TextSerializer().serialize(state)
    return state


def by_id(state: DOMState, element_id: str) -> tuple[int, DOMElementNode]:
    """The selector-map entry of the indexed element whose ``id`` is ``element_id``."""
    for key, node in state.selector_map.items():
        if node.attributes.get("id") == element_id:
            return key, node
    raise AssertionError(f"#{element_id} is not in the selector map:\n{state.serialized_dom}")


def indexed_ids(state: DOMState) -> set[str | None]:
    return {node.attributes.get("id") for node in state.selector_map.values()}
