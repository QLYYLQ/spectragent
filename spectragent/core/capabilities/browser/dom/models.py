"""DOM internal models — dataclass-based for performance.

Reference: browser-use EnhancedDOMTreeNode.

One page, several CDP targets
-----------------------------

A tab is not one DOM. Its main document, every same-process iframe and
every shadow root live in the tab's own renderer target and come back from
one ``DOM.getDocument(pierce=True)``. A cross-origin iframe that Chrome runs
out of process (an OOPIF) is a *different target*, reachable only through
its own CDP session, and its ``backendNodeId`` values are counted in a
different process. Two targets routinely hand out the same small ids, so a
``backend_node_id`` means nothing without the target it came from.

The tree built here is the composed page: a frame owner's
``content_document`` holds that frame's document whichever target it came
from, and every node records the session that owns it (``session_id``) —
the pair ``(session_id, backend_node_id)`` is what CDP understands.

The selector map needs a single ``int`` per element, because that integer
is the ``[index]`` the model reads and writes back. :func:`element_handle`
defines it: the tab's own target keeps ``backend_node_id`` as the key (so
a page without out-of-process frames is keyed exactly as before), and each
out-of-process frame target gets a namespace of its own above
:data:`FRAME_HANDLE_BASE`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import Field, dataclass, field, fields
from types import MappingProxyType
from typing import Any, Final

# ── trace serialization policy ─────────────────────────────────────
#
# These models are a graph, not a tree: ``parent`` points back up. Any
# JSON encoder therefore needs to be told where to stop, and *which*
# link is the back-reference is knowledge only this module has —
# ``parent`` and ``content_document`` are structurally identical
# declarations (``DOMElementNode | None``), so nothing downstream can
# infer it.
#
# Rather than let a consumer keep a list of our field names, a field
# declares its own policy: ``metadata=TRACE_SKIP`` means "leave me out
# of the run trace". The marker rides on the field, so renaming the
# field carries the decision with it — a name list in another module
# would silently start recording the full document instead.
#
# Three rules, in order:
#   1. a field whose name starts with ``_`` is private and never traced;
#   2. otherwise, ``metadata[TRACE_KEY] == "skip"`` excludes it;
#   3. ``metadata[TRACE_KEY] == "omit_none"`` (``TRACE_OMIT_NONE``) writes
#      the field only when it holds a value: ``None`` leaves the key out,
#      and a reader that finds no key reads the default. A field only one
#      strategy ever fills (``hit_test``, the ``skyvern_hit_test`` cleaner's)
#      thereby leaves every other strategy's trace JSON byte-identical.

TRACE_KEY: Final = "trace"
TRACE_SKIP: Final[Mapping[str, str]] = MappingProxyType({TRACE_KEY: "skip"})
TRACE_OMIT_NONE: Final[Mapping[str, str]] = MappingProxyType({TRACE_KEY: "omit_none"})


def is_traced(spec: Field[Any]) -> bool:
    """Whether ``spec`` belongs in the JSON that lands in the run trace."""
    return not spec.name.startswith("_") and spec.metadata.get(TRACE_KEY) != "skip"


def omitted_when_none(spec: Field[Any]) -> bool:
    """Whether a ``None`` in ``spec`` is left out of the trace JSON (rule 3)."""
    return spec.metadata.get(TRACE_KEY) == "omit_none"


def traced_fields(obj: Any) -> tuple[Field[Any], ...]:
    """Dataclass fields of ``obj`` that belong in the run trace."""
    return tuple(spec for spec in fields(obj) if is_traced(spec))


# ── selector-map keys ──────────────────────────────────────────────

FRAME_HANDLE_BASE: Final = 2**31
"""Width of one handle namespace.

Namespace ``n`` owns ``[n * 2**31, (n + 1) * 2**31)``, so no two namespaces
can produce the same key. Backend node ids are a per-renderer counter that
starts at 1 — reaching ``2**31`` would take two billion DevTools-tracked
nodes in one process — and :func:`element_handle` refuses an id that does
reach it instead of letting it spill into the next namespace. Namespace 0 is
the tab's own target, which is why its keys are its backend node ids
unchanged.
"""


def element_handle(namespace: int, backend_node_id: int) -> int | None:
    """The selector-map key of ``backend_node_id`` in handle namespace ``namespace``.

    ``namespace`` is 0 for the tab's own target (main document, same-process
    iframes, shadow roots) and the frame target's ordinal (1, 2, ...) for an
    out-of-process iframe. The ordinal is assigned once per frame for the
    life of the tab, so an element keeps its key across observations.

    Returns ``None`` for an id outside the namespace width: such a node
    cannot be given a key that is guaranteed not to collide, so it is not
    indexed rather than risking an index that clicks a different element.
    """
    if backend_node_id < 0 or backend_node_id >= FRAME_HANDLE_BASE:
        return None
    return namespace * FRAME_HANDLE_BASE + backend_node_id


@dataclass(slots=True)
class DOMRect:
    """Bounding rectangle in document or viewport coordinates."""

    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.width / 2, self.y + self.height / 2)

    def contains(self, other: DOMRect) -> bool:
        return (
            self.x <= other.x
            and self.y <= other.y
            and self.x + self.width >= other.x + other.width
            and self.y + self.height >= other.y + other.height
        )


@dataclass(frozen=True, slots=True)
class HitTest:
    """An in-page hit test of one indexed element (``skyvern_hit_test``, arch §4.12).

    Attributes:
        center_topmost: ``document.elementFromPoint`` at the centre of the
            element's visible rect returned the element, a descendant, or a
            ``<label>`` whose control it is — a click there reaches it.
        visible_point: A point a click reaches the element at, in CSS px of
            the top-level viewport: the centre when ``center_topmost``, else
            the first of a 5x5 grid inside the visible rect that hit, or
            ``None`` when none did — the element is fully covered.
    """

    center_topmost: bool
    visible_point: tuple[float, float] | None = None


@dataclass(slots=True)
class EnhancedAXProperty:
    """Single accessibility property from the AX tree."""

    name: str
    value: str | bool | float | None = None


@dataclass(slots=True)
class DOMElementNode:
    """Single DOM element with full selector and layout metadata."""

    # Core DOM
    tag_name: str
    node_type: int = 1  # 1=ELEMENT, 3=TEXT, 9=DOCUMENT, 11=DOCUMENT_FRAGMENT
    node_value: str = ""  # TEXT_NODE text content (from DOM.getDocument nodeValue)
    attributes: dict[str, str] = field(default_factory=dict)
    text_content: str = ""
    children: list[DOMElementNode] = field(default_factory=list)

    # Super-selector
    #
    # ``backend_node_id`` is unique only within the target that owns the
    # node; ``session_id`` names that target's CDP session and is what
    # every command about this node must be sent on. ``target_id`` is the
    # out-of-process frame target the node came from, ``None`` for the
    # tab's own target. ``frame_id`` is the frame whose document holds the
    # node (shadow-root content belongs to its host's document).
    node_id: int | None = None
    backend_node_id: int | None = None
    frame_id: str | None = None
    target_id: str | None = None
    session_id: str | None = None

    # Layout
    #
    # ``bounds`` is the border box in *page* coordinates: CSS pixels of the
    # top-level document, whichever frame the node lives in — a frame's
    # content is offset by its owner's content box and its own scroll. So
    # boxes from different frames are directly comparable, and for the
    # main document they are the usual document coordinates.
    # ``client_rect`` / ``scroll_rects`` are element-local metrics, not
    # positions: (clientLeft, clientTop, clientWidth, clientHeight) and
    # (scrollLeft, scrollTop, scrollWidth, scrollHeight).
    bounds: DOMRect | None = None
    client_rect: DOMRect | None = None
    scroll_rects: DOMRect | None = None
    is_visible: bool = True
    is_scrollable: bool = False

    # CSS computed styles
    computed_styles: dict[str, str] | None = None
    cursor_style: str | None = None

    # Accessibility
    ax_role: str | None = None
    ax_name: str | None = None
    ax_properties: list[EnhancedAXProperty] | None = None

    # Interactivity
    #
    # ``element_index`` is the node's selector-map key — the ``[index]`` the
    # serializers print and actions resolve — or ``None`` when the node is
    # not indexed. See :func:`element_handle`.
    element_index: int | None = None
    is_interactive: bool = False
    has_js_click_listener: bool = False
    is_new: bool = False  # True if element was not in previous snapshot

    # Display control
    should_display: bool = True  # False = skip tag in serialization but recurse children

    # Compound components (virtual sub-components for range, select, etc.)
    #
    # Private, so not traced by rule 1 — and nothing is lost: the text
    # serializer renders these into ``DOMState.serialized_dom``, which is
    # the form the agent actually read.
    _compound_children: list[dict[str, Any]] = field(default_factory=list)

    # Handle namespace of the target this node came from — 0 for the tab's
    # own target. Private: it is an input to ``element_index``, which is
    # the traced form, and it means nothing without the tab's frame table.
    _handle_namespace: int = 0

    # Iframe / Shadow DOM.
    #
    # Traced. These point *down* into content the agent genuinely saw —
    # the text serializer walks both into ``serialized_dom`` — so leaving
    # them out would mean a trace of an iframe-heavy page silently omits
    # the elements the run acted on. They are not back-references and not
    # a volume risk: node construction keeps shadow roots out of
    # ``children``, so each subtree is reached exactly once.
    #
    # ``content_document`` is the root of the frame's document whether the
    # frame runs in this target or out of process; its nodes carry the
    # session that owns them.
    content_document: DOMElementNode | None = field(default=None, repr=False)
    shadow_root_type: str | None = None
    shadow_roots: list[DOMElementNode] | None = None

    # Paint
    paint_order: int | None = None
    # Whether a click at the element's centre reaches it — recorded only by
    # the ``skyvern_hit_test`` cleaner; ``None`` (and absent from the trace
    # JSON, ``TRACE_OMIT_NONE``) everywhere else, and for an element that
    # was outside the viewport.
    hit_test: HitTest | None = field(default=None, metadata=TRACE_OMIT_NONE)

    # Tree nav
    #
    # Not traced: this is the one link that points *up*. Following it is
    # not a termination problem (the encoder's visited set handles that)
    # but a volume one — an encoder entering the graph at a leaf, which
    # is exactly how ``selector_map`` entries are encoded, walks straight
    # back to the document root and re-emits the whole page under every
    # entry (measured 17.7x on a 341-node page).
    parent: DOMElementNode | None = field(default=None, repr=False, metadata=TRACE_SKIP)

    def composed_children(self) -> list[DOMElementNode]:
        """Every subtree root below this node, in the order it is rendered.

        Light-DOM children, then shadow roots, then the frame's document —
        the order ``TextSerializer`` prints them in. A pass that walks only
        ``children`` sees neither shadow DOM nor frame content, which is how
        both used to reach the prompt without ever being indexed.
        """
        composed = list(self.children)
        if self.shadow_roots:
            composed.extend(self.shadow_roots)
        if self.content_document is not None:
            composed.append(self.content_document)
        return composed

    def descendants(self) -> list[DOMElementNode]:
        """All nodes below this one — shadow roots and frame documents included."""
        result: list[DOMElementNode] = []
        stack = self.composed_children()
        while stack:
            node = stack.pop()
            result.append(node)
            stack.extend(node.composed_children())
        return result

    def interactive_elements(self) -> list[DOMElementNode]:
        """Return all interactive descendant elements."""
        return [n for n in self.descendants() if n.is_interactive]

    def frame_owners(self) -> list[DOMElementNode]:
        """The ``<iframe>`` / ``<frame>`` elements this node sits inside, innermost first.

        Empty for a node of the top-level document. An action needs the
        chain because live geometry is relative to the node's own renderer:
        at each out-of-process boundary, the owner's content box — measured
        in the owner's renderer — lifts the point towards the page.
        """
        owners: list[DOMElementNode] = []
        node = self
        while node.parent is not None:
            if node.parent.content_document is node:
                owners.append(node.parent)
            node = node.parent
        return owners


DOMSelectorMap = dict[int, DOMElementNode]
"""``element_handle`` → node, for every indexed element of the page.

Keys are :func:`element_handle` values, which are the ``backend_node_id``
itself for the tab's own target. ``node.element_index`` repeats the key.
"""


@dataclass(slots=True)
class TargetCapture:
    """Raw CDP data for the documents of one renderer target.

    Everything here came over one CDP session, so its backend node ids form
    one namespace. ``frames`` holds the captures of this target's
    out-of-process child frames keyed by frame id — the ``frameId`` of the
    ``<iframe>`` node in ``raw_dom`` that hosts each one (for an out-of-process
    frame Chrome uses the frame id as the target id too).
    """

    raw_dom: dict[str, Any]
    snapshot: dict[str, Any]
    ax_tree: dict[str, Any]
    session_id: str | None = None
    #: The frame target's id; ``None`` for the tab's own target.
    target_id: str | None = None
    #: :func:`element_handle` namespace; 0 for the tab's own target.
    namespace: int = 0
    #: Backend ids with JS click listeners; ``None`` = detection skipped.
    js_click_listener_ids: set[int] | None = None
    frames: dict[str, TargetCapture] = field(default_factory=dict)
    #: The root frame's loader id and URL when this target was captured
    #: (``Page.getFrameTree``, read by both capture paths). The tab capture's
    #: pair is the DOM's document stamp for the cycle consistency check.
    loader_id: str | None = None
    url: str | None = None


@dataclass(slots=True)
class ScrollInfo:
    """Scroll state for a scrollable container or viewport."""

    scroll_x: float = 0.0
    scroll_y: float = 0.0
    scroll_width: float = 0.0
    scroll_height: float = 0.0
    viewport_width: float = 0.0
    viewport_height: float = 0.0

    @property
    def can_scroll_down(self) -> bool:
        return self.scroll_y + self.viewport_height < self.scroll_height

    @property
    def scroll_progress(self) -> float:
        max_scroll = self.scroll_height - self.viewport_height
        if max_scroll <= 0:
            return 1.0
        return self.scroll_y / max_scroll


@dataclass
class DOMState:
    """Complete DOM snapshot with selector map and scroll info."""

    dom_tree: DOMElementNode | None = None
    # Not traced: ``DOMObs`` carries the selector map as a field of its
    # own, and that copy is the trace contract. Encoding it here too
    # produced nothing but ``{"__cycle__": N}`` stubs, because every node
    # in it had already been reached through ``dom_tree``.
    selector_map: DOMSelectorMap = field(default_factory=dict, metadata=TRACE_SKIP)
    scroll_info: ScrollInfo = field(default_factory=ScrollInfo)
    serialized_dom: str = ""

    def get_element(self, index: int) -> DOMElementNode | None:
        """Look up an element by its selector-map key (its ``element_index``)."""
        return self.selector_map.get(index)
