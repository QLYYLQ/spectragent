"""Checkpoint restoration on Android: re-finding recorded elements, measuring restored screens.

``AndroidCapability`` implements ``RestorationSupport``
(``docs/architecture_design.md`` §4.9, §11.2 "分支回放") by delegating to the
pure functions here. They read only what they are handed: two observations of
the run, the recorded one (read back from the source trace) and the restored,
live one, each a whole ``NegotiatedObservation`` in which this capability's
fragments sit under their local names (``screenshot``, ``ui_tree``,
``foreground_app``). No step numbers, no trace import, no side effects.

**Retarget** (:func:`retarget_action`). An element index is the number the
cleaner gave an element of the capture the agent saw, and a restored screen
may number the same element differently (an element more or fewer before it).
Which actions name an element, and by which param, is what each of the
capability's own actions declares in its ``Params`` (``AddressesElement``,
read by ``core/actions/params.py::element_params``), never a list kept here.
The recorded element is re-found in the restored tree by its bounds, each of
the four edges within ``tolerance_px``, confirmed by its class, resource id,
text and description: exactly one such element rewrites that param
(``geometry_signature``), none is ``not_found`` and several are
``ambiguous``. Everything else (coordinate actions, keys, ``open_app``,
waits, a scroll given no index) runs as recorded (``identity``).

**Measure** (:func:`measure`). The app, activity and focused window of each
capture (its device record, ``observations.py::DeviceAtCapture``), the device
clocks, the multiset of the two trees' element signatures with the status
bar's elements left out, the keyboard and the input focus, and the Hamming
distance of the two screenshots' difference hashes (``core/image_hash.py``)
with the status bar painted out of both. The status bar is a system window
along the top edge of the screen, from its left edge, shorter than a quarter
of its width (:func:`status_bar`): its clock, battery and signal icons change
on their own. Every measure is ``None`` when what it reads is absent on either
side. The result is Android's own evidence type
(``restoration_evidence.py::AndroidRestorationEvidence``), which records the
limits the capability's knobs set and judges itself against them
(``failed_checks``); ``strict | warn`` is the restorer's.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Final

from PIL import Image, ImageDraw

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.actions.params import element_params
from spectragent.core.capabilities.android.device.forest import (
    AndroidAccessibilityForest,
    AndroidAccessibilityNodeInfo,
    AndroidAccessibilityWindowInfo,
    ProtoRect,
)
from spectragent.core.capabilities.android.observations import (
    FOREGROUND_APP,
    SCREENSHOT,
    UI_TREE,
    AndroidCaptureFragment,
    AndroidScreenshotObs,
    AndroidUITreeObs,
    DeviceAtCapture,
)
from spectragent.core.capabilities.android.restoration_evidence import (
    AndroidRestorationEvidence,
    ClockAlignment,
    InputState,
)
from spectragent.core.image_hash import grayscale, hamming, image_dhash
from spectragent.schemas.action import Action
from spectragent.schemas.observation._registrar import fragment_field_name
from spectragent.schemas.restoration import IDENTITY, Retarget

#: How a recorded element index is re-found (module docstring).
GEOMETRY_SIGNATURE: Final = "geometry_signature"
#: How many differing elements the evidence names, of each side.
NAMED_DIFFERENCES: Final = 10

#: (class, resource id, text, description): what confirms a re-found element.
ElementIdentity = tuple[str, str, str, str]
#: The identity, and whether the element is checked and selected: what a tree compares.
ElementSignature = tuple[str, str, str, str, bool, bool]
Box = tuple[int, int, int, int]

_SCREENSHOT_FIELD: Final = fragment_field_name(SCREENSHOT)
_UI_TREE_FIELD: Final = fragment_field_name(UI_TREE)
_CAPTURE_FIELDS: Final = tuple(
    fragment_field_name(key) for key in (SCREENSHOT, UI_TREE, FOREGROUND_APP)
)


# ── reading the observations ──────────────────────────────────────────


def screenshot_fragment(observation: object) -> AndroidScreenshotObs | None:
    """The observation's ``android.screenshot``, or ``None`` (not negotiated, or its capture failed)."""
    value = getattr(observation, _SCREENSHOT_FIELD, None)
    return value if isinstance(value, AndroidScreenshotObs) else None


def ui_tree_fragment(observation: object) -> AndroidUITreeObs | None:
    """The observation's ``android.ui_tree``, or ``None``."""
    value = getattr(observation, _UI_TREE_FIELD, None)
    return value if isinstance(value, AndroidUITreeObs) else None


def device_record(observation: object) -> DeviceAtCapture | None:
    """The device record of the capture that served *observation*, from whichever fragment carries it."""
    for name in _CAPTURE_FIELDS:
        value = getattr(observation, name, None)
        if isinstance(value, AndroidCaptureFragment) and value.device is not None:
            return value.device
    return None


# ── what a tree shows ─────────────────────────────────────────────────


def _box(rect: ProtoRect) -> Box:
    return rect.left, rect.top, rect.right, rect.bottom


def is_status_bar(window: AndroidAccessibilityWindowInfo) -> bool:
    """Whether *window* is the status bar: a system window at the screen's top-left, wide and low."""
    left, top, right, bottom = _box(window.bounds_in_screen)
    return (
        window.window_type == "TYPE_SYSTEM"
        and left == 0
        and top == 0
        and right > 0
        and 0 < (bottom - top) * 4 < right - left
    )


def status_bar(forest: AndroidAccessibilityForest) -> Box | None:
    """The status bar's bounds in screen pixels (:func:`is_status_bar`), or ``None`` without one."""
    for window in forest.windows:
        if is_status_bar(window):
            return _box(window.bounds_in_screen)
    return None


def element_identity(node: AndroidAccessibilityNodeInfo) -> ElementIdentity:
    """What confirms that a re-found element is the recorded one: class, resource id, text, description."""
    return node.class_name, node.view_id_resource_name, node.text, node.content_description


def element_signature(node: AndroidAccessibilityNodeInfo) -> ElementSignature:
    """What a tree comparison counts of an element: its identity, whether it is checked and selected.

    Never its index or its bounds: the same screen may number it otherwise.
    """
    return (*element_identity(node), node.is_checked, node.is_selected)


def describe(node: AndroidAccessibilityNodeInfo) -> str:
    """An element for a reader of the evidence: its class, its resource id, its text and description."""
    parts = [node.class_name.rsplit(".", 1)[-1] or "node"]
    if node.view_id_resource_name:
        parts.append(f"#{node.view_id_resource_name}")
    if node.text:
        parts.append(repr(node.text))
    if node.content_description and node.content_description != node.text:
        parts.append(f"({node.content_description!r})")
    if node.is_checked:
        parts.append("checked")
    if node.is_selected:
        parts.append("selected")
    return " ".join(parts)


def _elements_off_the_status_bar(tree: AndroidUITreeObs) -> list[AndroidAccessibilityNodeInfo]:
    bar = {window.id for window in tree.forest.windows if is_status_bar(window)}
    return [
        node
        for element, (_, node) in zip(tree.elements, tree.tree().element_nodes(), strict=True)
        if element.window_id not in bar
    ]


def input_state(tree: AndroidUITreeObs) -> InputState:
    """The keyboard and the node with input focus, as *tree* shows them."""
    keyboard = any(window.window_type == "TYPE_INPUT_METHOD" for window in tree.forest.windows)
    focused = next(
        (
            node
            for window in tree.forest.windows
            if window.window_type != "TYPE_INPUT_METHOD"
            for node in window.tree.nodes
            if node.is_focused
        ),
        None,
    )
    if focused is None:
        return InputState(keyboard_shown=keyboard)
    name = focused.class_name.rsplit(".", 1)[-1] or "node"
    if focused.view_id_resource_name:
        name += f" #{focused.view_id_resource_name}"
    left, top, right, bottom = _box(focused.bounds_in_screen)
    return InputState(
        keyboard_shown=keyboard, focused=f"{name} at [{left},{top}][{right},{bottom}]"
    )


# ── retarget ──────────────────────────────────────────────────────────


def _offset(recorded: ProtoRect, restored: ProtoRect) -> int:
    """How far apart two boxes are: the largest difference of an edge, in pixels."""
    return max(abs(a - b) for a, b in zip(_box(recorded), _box(restored), strict=True))


def _within(recorded: ProtoRect, restored: ProtoRect, tolerance_px: int) -> bool:
    return _offset(recorded, restored) <= tolerance_px


def retarget_action(
    action: Action,
    recorded: object,
    restored: object,
    *,
    actions: Mapping[str, ActionBase],
    tolerance_px: int,
) -> Retarget:
    """How the recorded *action* runs against the *restored* observation (module docstring).

    *actions* are the capability's own actions by name, each with its
    ``Params``; an action not among them names no element and runs as
    recorded. Computes only: nothing is executed and nothing is kept.
    """
    own = actions.get(action.name)
    named = element_params(own.Params) if own is not None else ()
    given = [param for param in named if action.params.get(param) is not None]
    if not given:
        return Retarget(action=action.model_copy(deep=True), method=IDENTITY)
    return _retarget_element(action, given[0], recorded, restored, tolerance_px)


def _retarget_element(
    action: Action, param: str, recorded: object, restored: object, tolerance_px: int
) -> Retarget:
    index = action.params[param]

    def not_found(detail: str) -> Retarget:
        return Retarget(action=None, method=GEOMETRY_SIGNATURE, failure="not_found", detail=detail)

    recorded_tree, restored_tree = ui_tree_fragment(recorded), ui_tree_fragment(restored)
    if recorded_tree is None or restored_tree is None:
        side = "recorded" if recorded_tree is None else "restored"
        return not_found(
            f"the {side} observation has no android.ui_tree to find element {index!r} in"
        )
    entry = recorded_tree.tree().node(index) if isinstance(index, int) else None
    if entry is None:
        return not_found(
            f"element {index!r} is not in the recorded UI tree "
            "(the recorded action could not have resolved it)"
        )
    identity = element_identity(entry)
    near = [
        (number, node)
        for number, node in restored_tree.tree().element_nodes()
        if _within(entry.bounds_in_screen, node.bounds_in_screen, tolerance_px)
    ]
    matches = [number for number, node in near if element_identity(node) == identity]
    what = describe(entry)
    if len(matches) == 1:
        copy = action.model_copy(deep=True)
        copy.params[param] = matches[0]
        return Retarget(
            action=copy, method=GEOMETRY_SIGNATURE, detail=f"element {index} -> {matches[0]} {what}"
        )
    if not matches:
        alike = [
            _offset(entry.bounds_in_screen, node.bounds_in_screen)
            for _, node in restored_tree.tree().element_nodes()
            if element_identity(node) == identity
        ]
        elsewhere = (
            f"; the nearest element that has them lies {min(alike)} px off"
            if alike
            else "; no element has them"
        )
        return not_found(
            f"no element within {tolerance_px} px of recorded element {index} {what} has its "
            f"class, resource id, text and description ({len(near)} candidate(s) by position"
            f"{elsewhere})"
        )
    return Retarget(
        action=None,
        method=GEOMETRY_SIGNATURE,
        failure="ambiguous",
        detail=f"{len(matches)} elements match recorded element {index} {what}",
    )


# ── measure ───────────────────────────────────────────────────────────


def _named(
    side: str, nodes: list[AndroidAccessibilityNodeInfo], surplus: Counter[ElementSignature]
) -> list[str]:
    """The first of *nodes* that *surplus* counts, named for a reader of the evidence."""
    left = surplus.copy()
    named: list[str] = []
    for node in nodes:
        signature = element_signature(node)
        if left[signature] > 0:
            left[signature] -= 1
            named.append(f"{side}: {describe(node)}")
    return named[:NAMED_DIFFERENCES]


def _tree_comparison(
    recorded: AndroidUITreeObs, restored: AndroidUITreeObs
) -> tuple[int, int, list[str]]:
    """How the two trees' elements differ, the status bar aside: missing, extra, and the first named."""
    before = _elements_off_the_status_bar(recorded)
    after = _elements_off_the_status_bar(restored)
    counted_before = Counter(map(element_signature, before))
    counted_after = Counter(map(element_signature, after))
    lost, gained = counted_before - counted_after, counted_after - counted_before
    named = _named("missing", before, lost) + _named("extra", after, gained)
    return lost.total(), gained.total(), named


def _paint_out(image: Image.Image, box: Box | None) -> Image.Image:
    if box is None:
        return image
    left, top, right, bottom = box
    right, bottom = min(right, image.width), min(bottom, image.height)
    if left >= right or top >= bottom:
        return image
    painted = image.copy()
    ImageDraw.Draw(painted).rectangle((left, top, right - 1, bottom - 1), fill=0)
    return painted


def _union(a: Box | None, b: Box | None) -> Box | None:
    if a is None or b is None:
        return a or b
    return min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])


def screenshot_distance(recorded: object, restored: object, bar: Box | None) -> int | None:
    """The two screenshots' difference-hash distance with *bar* painted out of both; ``None`` without both."""
    hashes: list[int] = []
    for observation in (recorded, restored):
        shot = screenshot_fragment(observation)
        gray = grayscale(shot.image_bytes) if shot is not None and shot.image_bytes else None
        if gray is None:
            return None
        hashes.append(image_dhash(_paint_out(gray, bar)))
    return hamming(hashes[0], hashes[1])


def measure(
    recorded: object,
    restored: object,
    *,
    alignment: ClockAlignment | None,
    screenshot_max_distance: int,
    clock_max_difference_s: float,
) -> AndroidRestorationEvidence:
    """What Android measures between a recorded and a restored observation (module docstring).

    *alignment* is what pinning the clock did before the restored capture
    (``None``: nothing was asked); the two limits are the capability's knobs,
    recorded with the evidence.
    """
    before, after = device_record(recorded), device_record(restored)
    foreground_match: bool | None = None
    clock_difference: int | None = None
    if before is not None and after is not None:
        foreground_match = (before.package, before.activity, before.window) == (
            after.package,
            after.activity,
            after.window,
        )
        clock_difference = after.clock_ms - before.clock_ms

    tree_match: bool | None = None
    missing = extra = 0
    differences: list[str] = []
    recorded_input = restored_input = None
    bar: Box | None = None
    recorded_tree, restored_tree = ui_tree_fragment(recorded), ui_tree_fragment(restored)
    if recorded_tree is not None:
        recorded_input = input_state(recorded_tree)
        bar = status_bar(recorded_tree.forest)
    if restored_tree is not None:
        restored_input = input_state(restored_tree)
        bar = _union(bar, status_bar(restored_tree.forest))
    if recorded_tree is not None and restored_tree is not None:
        missing, extra, differences = _tree_comparison(recorded_tree, restored_tree)
        tree_match = missing == 0 and extra == 0

    return AndroidRestorationEvidence(
        foreground_match=foreground_match,
        recorded_package=before.package if before is not None else None,
        restored_package=after.package if after is not None else None,
        recorded_activity=before.activity if before is not None else None,
        restored_activity=after.activity if after is not None else None,
        recorded_window=before.window if before is not None else None,
        restored_window=after.window if after is not None else None,
        recorded_clock_ms=before.clock_ms if before is not None else None,
        restored_clock_ms=after.clock_ms if after is not None else None,
        clock_difference_ms=clock_difference,
        clock_max_difference_s=clock_max_difference_s,
        clock_alignment=alignment,
        ui_tree_match=tree_match,
        ui_tree_missing=missing,
        ui_tree_extra=extra,
        ui_tree_differences=differences,
        recorded_input=recorded_input,
        restored_input=restored_input,
        screenshot_distance=screenshot_distance(recorded, restored, bar),
        screenshot_max_distance=screenshot_max_distance,
        status_bar=bar,
    )


__all__ = [
    "GEOMETRY_SIGNATURE",
    "NAMED_DIFFERENCES",
    "device_record",
    "element_identity",
    "element_signature",
    "input_state",
    "is_status_bar",
    "measure",
    "retarget_action",
    "screenshot_distance",
    "screenshot_fragment",
    "status_bar",
    "ui_tree_fragment",
]
