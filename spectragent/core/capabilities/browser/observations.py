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

The JSON shape a fragment writes is also the one it reads back: each
fragment here owns the inverse of its encoding (``from_trace``), so a
trace reader gets a ``DOMState`` and ``DOMElementNode`` entries, not the
encoder's dicts, and no downstream consumer writes a decoder of its own.

See docs/architecture_design.md §4.4 and §5.5.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import is_dataclass
from types import NoneType, UnionType
from typing import Any, ClassVar, Literal, Self, Union, get_args, get_origin, get_type_hints

from pydantic import Field, PrivateAttr, field_serializer

from spectragent.core.capabilities.browser.cursor import ScreenshotCursor, render_cursor
from spectragent.core.capabilities.browser.dom.budget import DOMBudgetReport
from spectragent.core.capabilities.browser.dom.models import (
    DOMElementNode,
    DOMState,
    omitted_when_none,
    traced_fields,
)
from spectragent.core.capabilities.browser.platform import InputPlatform
from spectragent.core.capabilities.browser.settle import PageSettle
from spectragent.core.capabilities.browser.tabs import FocusMove, FocusMoveReason
from spectragent.schemas.observation._registrar import FragmentBase
from spectragent.schemas.observation.capture_state import (
    CaptureStateFragment,
    ForegroundChange,
    ForegroundReason,
    SettleState,
)
from spectragent.schemas.observation.image import image_suffix

# ── DOM graph → JSON-safe dict ─────────────────────────────────────
#
# Pydantic's stock dataclass encoder raises ``Circular reference
# detected`` on a real ``DOMState``, so ``DOMObs`` encodes the graph
# itself. Two mechanisms, deliberately separate:
#
#   * which *links* to follow is declared on the dataclass fields
#     (``traced_fields``) — see ``dom/models.py``;
#   * which *nodes* have already been emitted is tracked here, per
#     encoding: one visited set for the whole ``element_tree``, and a fresh
#     one for **each** ``selector_map`` entry. Within one encoding a node
#     reached a second time becomes a ``{"__cycle__": <ref>}`` pointer
#     naming it — see ``_node_ref``. Across entries there are no pointers:
#     an indexed element nested under an earlier-indexed one (a button in
#     an indexed iframe) used to be written as a stub of its own entry,
#     with no ``bounds``, because the entries shared one set.
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
always lands first: the marker then names the node, and a list-valued
field is still a list of markers rather than turning into a dict and
changing shape under its reader.
"""


#: The encoder's two stand-ins for something it did not emit. ``_CYCLE``:
#: already written earlier in the same encoding. ``_TRUNCATED``: past the
#: depth budget. For a node the marker carries its ``_node_ref``; for a
#: value object, ``None``.
_CYCLE = "__cycle__"
_TRUNCATED = "__truncated__"


def _node_ref(node: DOMElementNode) -> dict[str, Any]:
    """How a pointer names ``node``: always the same three fields.

    ``backend_node_id`` means something only within one CDP target — an
    out-of-process frame hands out the same small ids as the page — so it
    travels with ``target_id`` (``None`` for the tab's own target), the pair
    every recorded node carries. ``element_index`` is the node's
    selector-map key when it is indexed, ``None`` when it is not; a reader
    never has to guess which kind of number it is holding.
    """
    return {
        "element_index": node.element_index,
        "backend_node_id": node.backend_node_id,
        "target_id": node.target_id,
    }


def _node_to_json(node: DOMElementNode, visited: set[int], depth: int) -> dict[str, Any]:
    nid = id(node)
    if nid in visited:
        return {_CYCLE: _node_ref(node)}
    if depth >= _MAX_ELEMENT_DEPTH:
        # Same shape as the cycle pointer, different reason: the subtree
        # exists and was not emitted. The reference is what lets a reader
        # say where the trace stopped.
        return {_TRUNCATED: _node_ref(node)}
    visited.add(nid)
    return {
        spec.name: _to_json(getattr(node, spec.name), visited, depth + 1)
        for spec in traced_fields(node)
        if not (omitted_when_none(spec) and getattr(node, spec.name) is None)
    }


def _to_json(value: Any, visited: set[int], depth: int = 0) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, DOMElementNode):
        return _node_to_json(value, visited, depth)
    if depth >= _MAX_ENCODE_DEPTH:
        return {_TRUNCATED: None}
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
            return {_CYCLE: None}
        visited.add(vid)
        try:
            return {
                spec.name: _to_json(getattr(value, spec.name), visited, depth + 1)
                for spec in traced_fields(value)
                if not (omitted_when_none(spec) and getattr(value, spec.name) is None)
            }
        finally:
            visited.discard(vid)
    if isinstance(value, dict):
        return {str(k): _to_json(v, visited, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json(v, visited, depth + 1) for v in value]
    return value


# ── JSON → DOM graph: the inverse, for a reader of the trace ───────
#
# Driven by the same declarations the encoder reads — each dataclass's
# traced fields and their annotations — so a field added to
# ``DOMElementNode`` decodes, typed, the day it lands, as long as its
# annotation is built from shapes the decoder knows: scalars, ``Any``,
# dataclasses, ``X | None``, unions of scalars, ``Literal``, ``list``,
# ``tuple``, ``set`` / ``frozenset`` and ``dict``. Any other generic
# (``Sequence[str]``, say) is a ``TypeError`` naming the annotation when the
# decode plan is built — a programming error that fails the first test to
# read a DOM back, not a trace error. A value whose JSON type does not match
# its annotation is a schema mismatch and raises ``ValueError``; a trace
# reader turns that into ``TraceSchemaError``.

_LOST = object()
"""What a ``__cycle__`` / ``__truncated__`` marker decodes to: no node.

Dropped from a list, ``None`` in an optional field, and a ``ValueError``
where the annotation requires a value.
"""


def _is_marker(value: Any) -> bool:
    """One of the encoder's stand-ins for a node it did not emit."""
    return type(value) is dict and len(value) == 1 and (_CYCLE in value or _TRUNCATED in value)


def _scalar_ok(value: Any, annotation: Any) -> bool:
    if annotation is Any:
        return True
    if annotation is bool:
        return isinstance(value, bool)
    if isinstance(value, bool):  # an int subclass, and never a number here
        return False
    if annotation is float:  # JSON writes an integral float without the point
        return isinstance(value, (int, float))
    return isinstance(value, annotation)


class _GraphDecoder:
    """Rebuilds the dataclasses ``_to_json`` encoded, one decode at a time.

    Decoders are built per field from the resolved annotation and kept for
    the life of this object only — one ``from_trace`` call — rather than in
    a module-level cache.
    """

    def __init__(self) -> None:
        self._plans: dict[type, dict[str, Callable[[Any], Any]]] = {}

    def dataclass(self, cls: type, value: Any) -> Any:
        """*value* as an instance of *cls*, or ``_LOST`` for a marker."""
        if _is_marker(value):
            return _LOST
        if not isinstance(value, dict):
            raise ValueError(
                f"a {cls.__name__} is recorded as a JSON object, not {type(value).__name__}"
            )
        plan = self._plan(cls)
        kwargs: dict[str, Any] = {}
        for name, item in value.items():
            decode = plan.get(name)
            if decode is None:
                raise ValueError(f"{cls.__name__} has no traced field {name!r}")
            kwargs[name] = decode(item)
        try:
            return cls(**kwargs)
        except TypeError as exc:  # a field without a default was not recorded
            raise ValueError(f"{cls.__name__} cannot be rebuilt from the trace: {exc}") from exc

    def _plan(self, cls: type) -> dict[str, Callable[[Any], Any]]:
        plan = self._plans.get(cls)
        if plan is None:
            hints = get_type_hints(cls)
            plan = {spec.name: self._field(hints[spec.name]) for spec in traced_fields(cls)}
            self._plans[cls] = plan
        return plan

    def _field(self, annotation: Any) -> Callable[[Any], Any]:
        """Decoder for a field's value; a marker where a value is required raises.

        Every generic origin is handled here or refused with ``TypeError``,
        and each branch recurses only into the annotation's arguments, so
        building a plan always terminates. (It used to hand an origin it
        did not know back to :meth:`_item`, which handed it straight back —
        a ``RecursionError`` on every read of any DOM fragment the day a
        field was annotated ``Literal[...]``.)
        """
        origin = get_origin(annotation)
        if origin is Union or origin is UnionType:
            options = [arg for arg in get_args(annotation) if arg is not NoneType]
            if len(options) == 1:
                inner = self._item(options[0])
                return lambda value: None if value is None else _none_if_lost(inner(value))
            return self._scalar_union(annotation, options)
        if origin is list or origin is set or origin is frozenset:
            item = self._item(get_args(annotation)[0])
            build: Callable[[Any], Any] = origin
            return lambda value: build(
                decoded for decoded in map(item, _expect(list, value)) if decoded is not _LOST
            )
        if origin is tuple:
            return self._tuple(annotation)
        if origin is dict:
            key_type, value_type = get_args(annotation)
            key = int if key_type is int else str
            decode_value = self._field(value_type)
            return lambda value: {key(k): decode_value(v) for k, v in _expect(dict, value).items()}
        if origin is Literal:
            return _literal(annotation)
        if origin is not None:
            raise TypeError(
                f"{annotation!r}: the DOM trace decoder has no rule for {origin!r} — "
                f"teach _GraphDecoder the shape before tracing a field of it"
            )
        if annotation in (tuple, set, frozenset):  # bare: no item type to decode by
            container: Callable[[Any], Any] = annotation
            return lambda value: container(_expect(list, value))
        inner = self._item(annotation)
        return lambda value: _kept(inner(value), annotation)

    def _tuple(self, annotation: Any) -> Callable[[Any], Any]:
        """``tuple[X, ...]`` decodes like a list; ``tuple[X, Y]`` holds exactly its items."""
        args = get_args(annotation)
        if len(args) == 2 and args[1] is Ellipsis:
            item = self._item(args[0])
            return lambda value: tuple(
                decoded for decoded in map(item, _expect(list, value)) if decoded is not _LOST
            )
        items = [self._field(arg) for arg in args]

        def decode(value: Any) -> tuple[Any, ...]:
            values = _expect(list, value)
            if len(values) != len(items):
                raise ValueError(
                    f"expected {annotation!r} ({len(items)} values), got {len(values)}"
                )
            return tuple(decode_item(v) for decode_item, v in zip(items, values, strict=True))

        return decode

    def _item(self, annotation: Any) -> Callable[[Any], Any]:
        """Decoder for one value of *annotation*, which may yield ``_LOST``."""
        if isinstance(annotation, type) and is_dataclass(annotation):
            return lambda value: self.dataclass(annotation, value)
        if get_origin(annotation) is not None:
            return self._field(annotation)

        def scalar(value: Any) -> Any:
            if _scalar_ok(value, annotation):
                return value
            raise ValueError(f"expected {annotation!r}, got {type(value).__name__} {value!r:.80}")

        return scalar

    @staticmethod
    def _scalar_union(annotation: Any, options: list[Any]) -> Callable[[Any], Any]:
        for option in options:
            if get_origin(option) is not None or is_dataclass(option):
                raise TypeError(f"{annotation!r}: only unions of scalars are decodable")
        nullable = NoneType in get_args(annotation)

        def scalar(value: Any) -> Any:
            if value is None and nullable:
                return None
            if any(_scalar_ok(value, option) for option in options):
                return value
            raise ValueError(f"expected {annotation!r}, got {type(value).__name__} {value!r:.80}")

        return scalar


def _literal(annotation: Any) -> Callable[[Any], Any]:
    """Decoder for ``Literal[...]``: one of the listed values, or a schema mismatch.

    ``True == 1`` in Python, so a bool must match a bool: ``Literal[1]``
    does not accept ``true``, nor ``Literal[True]`` ``1``.
    """
    allowed = get_args(annotation)

    def decode(value: Any) -> Any:
        for option in allowed:
            if value == option and isinstance(value, bool) == isinstance(option, bool):
                return option
        raise ValueError(f"expected one of {allowed!r}, got {type(value).__name__} {value!r:.80}")

    return decode


def _none_if_lost(value: Any) -> Any:
    return None if value is _LOST else value


def _kept(value: Any, annotation: Any) -> Any:
    if value is _LOST:
        raise ValueError(f"a required {annotation!r} was not recorded (cycle or truncation marker)")
    return value


def _expect(kind: type, value: Any) -> Any:
    if not isinstance(value, kind):
        raise ValueError(f"expected a JSON {kind.__name__}, got {type(value).__name__}")
    return value


def _relink(root: DOMElementNode) -> None:
    """Restore ``parent`` below *root*, the one link the encoding leaves out.

    Derived, not recorded: every child, shadow root and frame document
    below *root* gets the node it hangs from, exactly as the cleaners link
    them, so ``frame_owners()`` works inside the subtree. *root* keeps
    ``parent=None`` — what is above it is not in this encoding.
    """
    stack = [root]
    while stack:
        node = stack.pop()
        for child in node.composed_children():
            child.parent = node
            stack.append(child)


class BrowserCaptureFragment(FragmentBase):
    """What every fragment of one browser capture shares: how the capture waited, and on which tab.

    One capture serves every negotiated browser fragment of an observation
    cycle, after one settle wait (``settle.py``, §4.14). ``settle`` records
    that wait — set by the capability on each fragment of the capture it
    served, the same record on each, so a reader finds it on whichever
    browser fragment the run negotiated. ``None`` — and then left out of the
    trace JSON: the wait is turned off (``settle_max_wait_s: 0``), or the
    fragment was not served by an observation (a test built it).

    ``focus_moved`` is the same kind of record: the capability moved the
    agent's focus to another tab before this capture — it followed a tab the
    page opened during the agent's actions, or left a focused tab that closed
    itself (``tabs.py``, §4.14 "页面新开的标签页"). ``None`` — and then left
    out of the trace JSON — when the focus stayed where it was, so an
    observation without a move writes what it always wrote. No agent renders
    either field; :class:`BrowserCaptureStateObs` projects both into the
    platform-neutral capture state an agent may read.
    """

    __abstract__ = True

    settle: PageSettle | None = None
    focus_moved: FocusMove | None = None

    def trace_json_exclude(self) -> set[str]:
        # Left out of the trace when there is no record, so a fragment that was
        # never served by a waiting observation, or captured the tab the agent
        # was already on, writes what it always wrote.
        excluded: set[str] = set()
        if self.settle is None:
            excluded.add("settle")
        if self.focus_moved is None:
            excluded.add("focus_moved")
        return excluded


class DOMObs(BrowserCaptureFragment):
    """DOM tree observation from browser capability.

    **What the trace keeps, and what it cannot.** ``element_tree`` (a
    ``DOMState``) and ``selector_map`` (handle → ``DOMElementNode``) are
    written by ``_to_json`` and read back as those types by
    :meth:`from_trace`. Each ``selector_map`` entry is written in full —
    tag, attributes, ``bounds``, its whole subtree and therefore its text,
    which the BrowserUse cleaner keeps in descendant ``#text`` nodes rather
    than in ``text_content`` — so an entry is usable on its own, without
    the tree. That costs size, growing with how deeply indexed elements
    nest inside one another: re-encoding the 38 non-empty M18 DOM files
    (BrowserUse and Skyvern-JS cleaning) grew them by a median 14% and at
    most 65% pretty-printed (31% / 98% compact), and a 34.6 MB Skyvern-JS
    page became ~50 MB. The encoding is still lossy in four ways, and the
    read side reports each loss rather than inventing what is missing:

    - **depth**: past ``_MAX_ELEMENT_DEPTH`` encoder levels a subtree is a
      ``{"__truncated__": ref}`` marker. The node it names is not
      rebuilt; it is simply absent from the decoded children.
    - **repeats inside one encoding**: a node reached twice within the
      tree, or within one entry, is a ``{"__cycle__": ref}`` pointer the
      second time. The pointer is dropped on read; the node appears once,
      where it was first written.
    - **``parent``** is never written. :meth:`from_trace` relinks it inside
      each decoded subtree, so ``frame_owners()`` works within the tree;
      the root of a ``selector_map`` entry has ``parent=None``, because
      nothing above it is in the entry.
    - **private fields** (``_compound_children``, ``_handle_namespace``)
      are not written and read back as their defaults. The text the
      compound controls rendered to is in ``serialized_dom``, which is
      what the agent read.

    ``DOMState.selector_map`` is not written separately; after a read it
    is the same mapping as this fragment's ``selector_map``. The decoded
    tree and the decoded entries are distinct objects — an entry is a
    copy of its node, not a pointer into ``dom_tree``.
    """

    __registry_key__ = "browser.dom"
    __source__: ClassVar[Literal["env", "loop"]] = "env"

    element_tree: Any = None  # DOMState at type-check time
    # Keys are element handles (int) — the ``backend_node_id`` for the
    # tab's own target, namespaced for an out-of-process frame; see
    # ``DOMSelectorMap`` / ``element_handle`` in ``dom/models.py``. Each
    # entry repeats its key as ``element_index`` and names the CDP session
    # that owns it. This is the trace contract for "what was actionable at
    # this step"; the copy nested in ``DOMState`` is not traced. The
    # serializer stringifies keys for JSON output.
    selector_map: dict[int, Any] = Field(default_factory=dict)
    # What a DOM budget removed on the way here, or ``None`` when nothing
    # was removed. Without it, ``42 interactive elements`` is ambiguous
    # between "the page had 42" and "the page had 900 and the run paid for
    # 42", and an evaluator reading the trace cannot tell them apart — the
    # lying-trace defect. ``DOMWatchdog`` sets it; see
    # ``dom/budget.py``.
    budget: DOMBudgetReport | None = None

    def summary(self) -> str:
        base = f"{len(self.selector_map)} interactive elements"
        if self.budget is None:
            return base
        return f"{base} ({self.budget.summary()})"

    @field_serializer("element_tree")
    def _serialize_element_tree(self, value: Any) -> Any:
        return _to_json(value, set())

    @field_serializer("selector_map")
    def _serialize_selector_map(self, value: dict[int, Any]) -> dict[str, Any]:
        # A visited set per entry, not one for the map: every entry must be
        # readable on its own terms — not as a pointer into the
        # ``element_tree`` encoding, and not as a pointer into an earlier
        # entry. Sharing one set wrote every indexed element nested under
        # an earlier-indexed one (the contents of an indexed iframe) as a
        # ``__cycle__`` stub with no bounds — most entries of a BrowserUse
        # page. The price is that a nested element's subtree is written
        # once per entry that contains it.
        return {str(k): _to_json(v, set()) for k, v in value.items()}

    @classmethod
    def from_trace(cls, payload: dict[str, Any], artifact: bytes | None) -> Self:
        """Read the fragment back with its graph typed: ``DOMState`` and nodes.

        ``element_tree`` comes back as a ``DOMState`` (``serialized_dom``,
        ``scroll_info``, ``dom_tree``) whose ``selector_map`` is this
        fragment's ``selector_map``; each entry comes back as a
        ``DOMElementNode`` keyed by its integer handle. What the encoding
        lost stays lost — see the class docstring.

        A JSON scalar where the graph would be (``None``, or a string a
        test put there) is returned as written; only a JSON object is a
        graph. An entry that is itself a marker is refused: entries are
        written in full, so a stub entry means the bytes are not this
        encoding.

        Raises:
            ValueError: The payload is not this fragment's encoding.
        """
        decoder = _GraphDecoder()

        raw_map = payload.get("selector_map") or {}
        if not isinstance(raw_map, dict):
            raise ValueError(
                f"selector_map is recorded as a JSON object, not {type(raw_map).__name__}"
            )
        selector_map = {
            int(key): _decode_entry(decoder, key, entry) for key, entry in raw_map.items()
        }

        tree = payload.get("element_tree")
        if isinstance(tree, dict):
            tree = _decode_state(decoder, tree)
        elif isinstance(tree, list):
            raise ValueError("element_tree is recorded as a DOMState object, not a JSON array")

        fragment = super().from_trace(
            {**payload, "element_tree": tree, "selector_map": selector_map}, artifact
        )
        if isinstance(fragment.element_tree, DOMState):
            # Validation copied the dict; point the state at the copy the
            # fragment holds, so there is one map, as at capture time.
            fragment.element_tree.selector_map = fragment.selector_map
        return fragment


def _decode_state(decoder: _GraphDecoder, value: dict[str, Any]) -> DOMState:
    state = decoder.dataclass(DOMState, value)
    if state is _LOST:
        raise ValueError("element_tree is a cycle/truncation marker, not a DOMState")
    if state.dom_tree is not None:
        _relink(state.dom_tree)
    return state


def _decode_entry(decoder: _GraphDecoder, key: str, value: Any) -> Any:
    if not isinstance(value, dict):
        if isinstance(value, list):
            raise ValueError(f"selector_map entry {key} is a JSON array, not an element")
        return value
    node = decoder.dataclass(DOMElementNode, value)
    if node is _LOST:
        raise ValueError(
            f"selector_map entry {key} is a {next(iter(value))} marker, not an element: "
            f"entries are written in full, so these bytes are not this fragment's encoding"
        )
    _relink(node)
    return node


class BrowserPlatformObs(FragmentBase):
    """The measured operating system of the browser receiving input events."""

    __registry_key__ = "browser.platform"
    __source__: ClassVar[Literal["env", "loop"]] = "env"
    operating_system: InputPlatform

    def summary(self) -> str:
        return self.operating_system


class ScreenshotObs(BrowserCaptureFragment):
    """Screenshot of the browser viewport.

    Either ``image_path`` (on disk) or ``image_bytes`` (in memory) must be
    set; the model view prefers ``image_bytes`` when both are present. The
    encoding is PNG unless the capability was configured for JPEG
    (``screenshot_format``), and is not carried as a field: the bytes
    already say, and a second source of truth can disagree with them —
    see :mod:`spectragent.schemas.observation.image`.

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
    ``viewport_width / image_width``; ``0`` means the size is unknown. The
    four fields are what make this fragment a
    :class:`~spectragent.schemas.observation.geometry.ScreenshotGeometry`.
    """

    __registry_key__ = "browser.screenshot"
    __source__: ClassVar[Literal["env", "loop"]] = "env"

    image_path: str | None = None
    image_bytes: bytes | None = None
    viewport_width: int = 0
    viewport_height: int = 0
    image_width: int = 0
    image_height: int = 0
    source_url: str | None = Field(
        default=None,
        description="The document URL read in the same capture cycle; None when unavailable.",
    )

    #: None means disabled; a cursor with no position means enabled but unknown.
    cursor: ScreenshotCursor | None = None
    _model_image_cache: tuple[tuple[Any, ...], bytes] | None = PrivateAttr(default=None)

    def model_image_bytes(self) -> bytes | None:
        """The model presentation, derived from clean evidence and immutable metadata.

        The cache belongs to this fragment and keys every rendering input; mutable
        fragment fields can never reuse a presentation of an earlier capture.
        """
        image = self.image_bytes
        if image is None and self.image_path:
            from pathlib import Path

            image = Path(self.image_path).read_bytes()
        if image is None or self.cursor is None:
            return image
        key = (image, self.cursor, self.viewport_width, self.viewport_height)
        if self._model_image_cache is None or self._model_image_cache[0] != key:
            self._model_image_cache = (
                key,
                render_cursor(
                    image,
                    self.cursor,
                    viewport_width=self.viewport_width,
                    viewport_height=self.viewport_height,
                ),
            )
        return self._model_image_cache[1]

    @field_serializer("image_bytes")
    def _serialize_image_bytes(self, value: bytes | None) -> str | None:
        if value is None:
            return None
        return base64.b64encode(value).decode("ascii")

    def trace_binary(self) -> tuple[str, bytes] | None:
        """Persist the image as a real file, not base64 inside JSON.

        The extension comes from the bytes' own header, so a run
        configured for JPEG writes ``NNN_browser.screenshot.jpg`` and an
        evaluator that opens it by suffix gets the media type right.
        Bytes of no recognised encoding fall back to ``.png``: the
        capability can only ask CDP for png or jpeg, so this is a
        truncated capture, and the trace is more useful naming it like its
        neighbours than under a suffix no evaluator expects.

        No bytes — ``None`` or empty — means no artifact. The trace reads a
        committed empty artifact as damage (a file the WAL wrote durably
        cannot be empty), so writing one made a single blank frame turn
        ``RunTrace.screenshots`` into an error for the whole run. The
        resolver raises on an empty capture instead (``ScreenshotWatchdog``);
        a fragment that still carries ``b""`` reads back with
        ``image_bytes=None`` — no image either way, and no frame for that
        step.
        """
        if not self.image_bytes:
            return None
        return image_suffix(self.image_bytes) or ".png", self.image_bytes

    def trace_json_exclude(self) -> set[str]:
        return super().trace_json_exclude() | {"image_bytes"}

    @classmethod
    def from_trace(cls, payload: dict[str, Any], artifact: bytes | None) -> Self:
        """The inverse of :meth:`trace_binary`: the artifact *is* ``image_bytes``.

        ``None`` when the capture carried no bytes (an ``image_path``-only
        fragment, or an empty capture, wrote no artifact). The sidecar never
        holds the image, so the artifact is the only source — whatever the
        payload says about ``image_bytes`` is ignored.
        """
        return cls.model_validate({**payload, "image_bytes": artifact})

    def summary(self) -> str:
        if not self.viewport_width and not self.viewport_height:
            return ""
        return f"{self.viewport_width}x{self.viewport_height}"


class BrowserMetaObs(BrowserCaptureFragment):
    """Browser metadata: URL, title, tab info."""

    __registry_key__ = "browser.browser_meta"
    __source__: ClassVar[Literal["env", "loop"]] = "env"

    url: str = ""
    page_title: str = ""
    tabs: list[dict[str, Any]] = Field(default_factory=list)
    #: The main frame's loader id when ``url`` was read — which document the
    #: URL belongs to (§4.14). ``None`` when the frame tree was unavailable.
    loader_id: str | None = None

    def summary(self) -> str:
        if not self.url and not self.page_title:
            return ""
        return f"{self.page_title or '(untitled)'} <{self.url}>"


#: How the browser's tab-focus record reads in the platform-neutral capture state.
_FOREGROUND_REASONS: dict[FocusMoveReason, ForegroundReason] = {
    "new_tab": "opened",
    "tab_closed": "closed",
}


class BrowserCaptureStateObs(BrowserCaptureFragment, CaptureStateFragment):
    """How the browser stood when this observation was captured (``capture_state``, §4.4).

    The platform-neutral facts of :class:`CaptureStateFragment`, for the
    browser. Two of them are records every browser fragment of a capture
    already carries, and are projected from them rather than measured a
    second time: :attr:`settle_state` reads :attr:`settle` (the one settle
    wait of the capture) and :attr:`foreground_change` reads
    :attr:`focus_moved` (the tab the capability followed or left before the
    capture). The capability assigns both after the resolvers answered, the
    same record on every fragment of the capture, so the projections are
    computed on access and never stored: the trace keeps ``settle`` and
    ``focus_moved`` exactly as on the other browser fragments, and nothing
    else of them.

    The other two are this fragment's own fields, set by its resolver
    (``watchdogs/capture_state_watchdog.py``): :attr:`pointer`, the tab's last
    confirmed pointer position, and :attr:`failed_visible_resources`, the
    images visible in the viewport that failed to load.
    """

    __registry_key__ = "browser.capture_state"
    __source__: ClassVar[Literal["env", "loop"]] = "env"

    @property
    def settle_state(self) -> SettleState | None:
        """The capture's settle wait; ``None`` when the wait is off (``settle_max_wait_s: 0``)."""
        settle = self.settle
        if settle is None:
            return None
        return SettleState(
            outcome=settle.outcome,
            waited_ms=settle.waited_ms,
            pending=tuple(settle.waiting_for),
        )

    @property
    def foreground_change(self) -> ForegroundChange | None:
        """The tab move the page caused before the capture; ``None`` when the focus stayed."""
        move = self.focus_moved
        if move is None:
            return None
        return ForegroundChange(
            reason=_FOREGROUND_REASONS[move.reason],
            from_surface=move.from_tab,
            to_surface=move.to_tab,
        )
