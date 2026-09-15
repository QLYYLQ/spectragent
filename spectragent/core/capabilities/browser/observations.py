"""Observation fragments provided by the Browser capability.

A fragment owns its own JSON shape, the same way an action owns its own
params (see :mod:`spectragent.core.actions.base`). ``DOMObs`` cannot be
encoded without knowing that ``DOMElementNode`` is a cyclic graph, so it
lives here — inside the package that owns ``DOMElementNode`` — and the
shared :mod:`spectragent.schemas.observation` package names no field of
any domain. This mirrors ``core/actions/`` (protocol + registrar) vs
``core/capabilities/browser/actions/`` (the concrete units).

Adding a fragment is still one class: declare it here, have the
capability's Resolver (a Watchdog) return it. ``BrowserCapability``
derives its ``provides`` set from the watchdogs, so it does not change.

See docs/architecture_design.md §4.4.
"""

from __future__ import annotations

import base64
from dataclasses import is_dataclass
from typing import Any, ClassVar, Literal

from pydantic import Field, field_serializer

from spectragent.core.capabilities.browser.dom.models import (
    DOMElementNode,
    traced_fields,
)
from spectragent.schemas.observation._registrar import FragmentBase

# ── DOM graph → JSON-safe dict ─────────────────────────────────────
#
# Pydantic's stock dataclass encoder raises ``Circular reference
# detected`` on a real ``DOMState``, so ``DOMObs`` encodes the graph
# itself. Two mechanisms, deliberately separate:
#
#   * which *links* to follow is declared on the dataclass fields
#     (``traced_fields``) — see ``dom/models.py``;
#   * which *nodes* have already been emitted is tracked here, because
#     the same node is reachable from several roots (``selector_map``
#     entries are also nodes of ``dom_tree``). A repeat becomes a
#     ``{"__cycle__": <backend_node_id>}`` pointer, which a reader can
#     resolve against the selector map.
#
# Depth is the third, and it is not a style choice. ``model_dump_json``
# is a pydantic-core call, and pydantic-core's serializer gives up past
# roughly 256 levels of output nesting with ``Circular reference
# detected`` — a lie, but a fatal one: ``TraceWriter`` is a blocking hook
# on ``record_step``, so it takes the run down at step 0. The encoder
# emits about two levels per DOM level and an iframe's or shadow root's
# nesting adds to its host's, so a budget below that ceiling is what
# makes the outcome depend on the page rather than on a library's
# recursion guard.

_MAX_ELEMENT_DEPTH = 200
"""Encoder levels, not DOM levels — roughly 90 nested elements.

Deliberately under pydantic-core's ~256: the truncation has to fire
before the serializer does, or the budget buys nothing. The deepest
combined nesting measured on real pages (cnn.com, ad-iframe heavy) is 40
elements, so this is ~2x headroom over anything a run is likely to see.
"""

_MAX_ENCODE_DEPTH = _MAX_ELEMENT_DEPTH + 8
"""Backstop for nesting that is not elements — dicts, lists, value objects.

Above the element budget on purpose, so on a deep page the element cut
always lands first: the marker then carries a ``backend_node_id``, and a
list-valued field is still a list of markers rather than turning into a
dict and changing shape under its reader.
"""


def _node_to_json(
    node: DOMElementNode, visited: set[int], depth: int
) -> dict[str, Any]:
    nid = id(node)
    if nid in visited:
        return {"__cycle__": node.backend_node_id}
    if depth >= _MAX_ELEMENT_DEPTH:
        # Same shape as the cycle pointer, different reason: the subtree
        # exists and was not emitted. The id is what lets a reader say
        # where the trace stopped.
        return {"__truncated__": node.backend_node_id}
    visited.add(nid)
    return {
        spec.name: _to_json(getattr(node, spec.name), visited, depth + 1)
        for spec in traced_fields(node)
    }


def _to_json(value: Any, visited: set[int], depth: int = 0) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, DOMElementNode):
        return _node_to_json(value, visited, depth)
    if depth >= _MAX_ENCODE_DEPTH:
        return {"__truncated__": None}
    if is_dataclass(value) and not isinstance(value, type):
        # Plain value objects (DOMRect, ScrollInfo, EnhancedAXProperty).
        # Unlike nodes they may legitimately repeat — one DOMRect can
        # hang off several nodes — so their id is held only for the
        # duration of their own subtree. That still cannot recurse
        # forever, which the previous encoder could: it walked unknown
        # dataclasses with no guard at all, so one unexpected shape meant
        # a RecursionError in the middle of a WAL write.
        vid = id(value)
        if vid in visited:
            return {"__cycle__": None}
        visited.add(vid)
        try:
            return {
                spec.name: _to_json(getattr(value, spec.name), visited, depth + 1)
                for spec in traced_fields(value)
            }
        finally:
            visited.discard(vid)
    if isinstance(value, dict):
        return {str(k): _to_json(v, visited, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json(v, visited, depth + 1) for v in value]
    return value


class DOMObs(FragmentBase):
    """DOM tree observation from browser capability."""

    __registry_key__ = "browser.dom"
    __source__: ClassVar[Literal["env", "loop"]] = "env"

    element_tree: Any = None  # DOMState at type-check time
    # Keys are ``backend_node_id`` (int) — see ``DOMSelectorMap`` in
    # ``dom/models.py``. This is the trace contract for "what was
    # actionable at this step"; the copy nested in ``DOMState`` is not
    # traced. The serializer stringifies keys for JSON output.
    selector_map: dict[int, Any] = Field(default_factory=dict)

    def summary(self) -> str:
        return f"{len(self.selector_map)} interactive elements"

    @field_serializer("element_tree")
    def _serialize_element_tree(self, value: Any) -> Any:
        return _to_json(value, set())

    @field_serializer("selector_map")
    def _serialize_selector_map(self, value: dict[int, Any]) -> dict[str, Any]:
        # A visited set of its own: entries must be readable on their own
        # terms, not as pointers into the ``element_tree`` encoding.
        visited: set[int] = set()
        return {str(k): _to_json(v, visited) for k, v in value.items()}


class ScreenshotObs(FragmentBase):
    """Screenshot of the browser viewport.

    Either ``image_path`` (on-disk PNG) or ``image_bytes`` (in-memory PNG)
    must be set; consumers prefer ``image_path`` when both are present.

    A desktop or mobile capability that wants a screenshot registers its
    own fragment under its own key rather than reusing this one — two
    active fragments whose local name collides are rejected at
    negotiation, so ``browser.screenshot`` and ``desktop.screenshot``
    could never both be live in one run anyway.

    Two sizes, because they are two different grids. ``viewport_*`` is the
    CSS-pixel viewport — the space every ``browser.*`` coordinate action
    executes in (``Input.dispatchMouseEvent`` takes CSS pixels).
    ``image_*`` is the pixel size of the image itself: ``Page.captureScreenshot``
    renders the viewport at the device scale factor, so on a DPR-2 display
    the image is twice the viewport in each direction. A consumer that shows
    the image to a model and takes a point back converts with
    ``viewport_width / image_width``; ``0`` means the size is unknown.
    """

    __registry_key__ = "browser.screenshot"
    __source__: ClassVar[Literal["env", "loop"]] = "env"

    image_path: str | None = None
    image_bytes: bytes | None = None
    viewport_width: int = 0
    viewport_height: int = 0
    image_width: int = 0
    image_height: int = 0

    @field_serializer("image_bytes")
    def _serialize_image_bytes(self, value: bytes | None) -> str | None:
        if value is None:
            return None
        return base64.b64encode(value).decode("ascii")

    def trace_binary(self) -> tuple[str, bytes] | None:
        """Persist the PNG as a real ``.png`` file, not base64 inside JSON."""
        if self.image_bytes is None:
            return None
        return ".png", self.image_bytes

    def trace_json_exclude(self) -> set[str]:
        return {"image_bytes"}

    def summary(self) -> str:
        if not self.viewport_width and not self.viewport_height:
            return ""
        return f"{self.viewport_width}x{self.viewport_height}"


class BrowserMetaObs(FragmentBase):
    """Browser metadata: URL, title, tab info."""

    __registry_key__ = "browser.browser_meta"
    __source__: ClassVar[Literal["env", "loop"]] = "env"

    url: str = ""
    page_title: str = ""
    tabs: list[dict[str, Any]] = Field(default_factory=list)

    def summary(self) -> str:
        if not self.url and not self.page_title:
            return ""
        return f"{self.page_title or '(untitled)'} <{self.url}>"
