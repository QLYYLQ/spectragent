"""Checkpoint restoration for the browser: re-finding recorded targets, measuring restored pages.

``BrowserCapability`` implements ``RestorationSupport``
(``docs/architecture_design.md`` §4.9, §4.14 "恢复：重定位与校验") by
delegating to the pure functions here. They read only what they are handed:
two observations of the run — the recorded one (read back from the source
trace by ``DOMObs.from_trace`` and its siblings) and the restored, live one —
each a whole ``NegotiatedObservation`` in which this capability's fragments
sit under their local names (``dom``, ``browser_meta``, ``screenshot``). No
step numbers, no trace import, no side effects.

**Retarget** (:func:`retarget_action`). A browser element index is Chrome's
``backendNodeId`` (``dom/models.py::element_handle``), and a freshly started
browser numbers the same element differently, so a recorded
``click(index)`` cannot run as written. Index actions are re-found by
geometry, then confirmed by signature: the recorded ``selector_map`` entry's
``bounds`` (page coordinates) pick the restored candidates whose four bounds
values all lie within ``tolerance_px``; the candidates whose
:func:`element_signature` equals the recorded entry's are the matches;
exactly one rewrites ``params["index"]`` (``geometry_signature``), none is
``not_found`` and several are ``ambiguous``. ``browser.switch_tab``'s
``target_id`` (a CDP targetId, fresh per browser) is re-found by its
position in ``browser_meta.tabs`` and that position's URL
(``tab_order_url``). Everything else — coordinate actions, navigation, keys,
waits, an index action without an index — runs as recorded (``identity``):
pinned replay makes the layout deterministic, and the screenshot hash checks
it.

**Measure** (:func:`measure`). URL equality, the multiset of interactive
element signatures, the Hamming distance between the two screenshots'
64-bit difference hashes (:func:`dhash`), the main-frame document
misses the capability counted since its last measurement, and the two
screenshots' media playback states with the media alignment done before the
restored capture (:func:`media_measure`, ``media.py``). Every measure is
``None`` when its fragment (or its bytes) is absent on either side. Never a
verdict: thresholds and ``strict | warn`` are the restorer's.
"""

from __future__ import annotations

import hashlib
import io
import logging
from collections import Counter
from collections.abc import Iterator
from typing import Any, Final

from PIL import Image, UnidentifiedImageError

from spectragent.core.capabilities.browser.dom.models import DOMElementNode, DOMRect
from spectragent.core.capabilities.browser.media import media_evidence
from spectragent.core.capabilities.browser.observations import (
    BrowserMetaObs,
    DOMObs,
    ScreenshotObs,
)
from spectragent.schemas.action import Action
from spectragent.schemas.observation._registrar import fragment_field_name
from spectragent.schemas.restoration import (
    MediaAlignment,
    MediaEvidence,
    RestorationEvidence,
    Retarget,
)

logger = logging.getLogger(__name__)

#: Actions whose ``index`` param always names a ``selector_map`` element.
INDEX_ACTIONS: Final = frozenset({"browser.click", "browser.type", "browser.select_dropdown"})
#: Actions that name an element only when they carry an ``index``.
OPTIONAL_INDEX_ACTIONS: Final = frozenset({"browser.scroll", "browser.extract_content"})
#: The action whose ``target_id`` is a per-browser CDP targetId.
SWITCH_TAB: Final = "browser.switch_tab"

TEXT_DEPTH_LIMIT: Final = 64
"""How many DOM levels below an element its signature text reads.

The trace encodes a ``selector_map`` entry's subtree down to roughly 100
levels (``observations._MAX_ELEMENT_DEPTH`` encoder levels, two per level of
``children``) and drops what lies deeper; a live element has its whole
subtree. Reading no deeper than this on both sides makes the recorded and
the restored signature of one element read the same nodes. Real pages
measure at most ~40 combined levels (the encoder's note), so nothing a run
sees is cut.
"""

#: (tag, id, name, aria-label, role, sha256 of the whitespace-folded subtree text).
ElementSignature = tuple[str, str | None, str | None, str | None, str | None, str]

_DOM_FIELD: Final = fragment_field_name(DOMObs.__registry_key__)
_META_FIELD: Final = fragment_field_name(BrowserMetaObs.__registry_key__)
_SCREENSHOT_FIELD: Final = fragment_field_name(ScreenshotObs.__registry_key__)


# ── reading the observations ──────────────────────────────────────────


def dom_fragment(observation: Any) -> DOMObs | None:
    """The observation's ``browser.dom``, or ``None`` (not negotiated, or its capture failed)."""
    value = getattr(observation, _DOM_FIELD, None)
    return value if isinstance(value, DOMObs) else None


def meta_fragment(observation: Any) -> BrowserMetaObs | None:
    """The observation's ``browser.browser_meta``, or ``None``."""
    value = getattr(observation, _META_FIELD, None)
    return value if isinstance(value, BrowserMetaObs) else None


def screenshot_fragment(observation: Any) -> ScreenshotObs | None:
    """The observation's ``browser.screenshot``, or ``None``."""
    value = getattr(observation, _SCREENSHOT_FIELD, None)
    return value if isinstance(value, ScreenshotObs) else None


# ── element signatures ────────────────────────────────────────────────


def _text_parts(node: DOMElementNode, depth: int) -> Iterator[str]:
    if node.text_content:
        yield node.text_content
    if node.node_type == 3 and node.node_value:
        yield node.node_value
    if depth >= TEXT_DEPTH_LIMIT:
        return
    for child in node.composed_children():
        yield from _text_parts(child, depth + 1)


def subtree_text(node: DOMElementNode) -> str:
    """The text of *node* and its subtree (shadow roots and frame documents included), whitespace folded.

    Elements keep their text in ``text_content`` (the Skyvern cleaner) or in
    descendant ``#text`` nodes (the BrowserUse cleaner); both are read, in
    document order, to :data:`TEXT_DEPTH_LIMIT` levels.
    """
    return " ".join(" ".join(_text_parts(node, 0)).split())


def element_signature(node: DOMElementNode) -> ElementSignature:
    """What identifies an element apart from its index and its position.

    The tag, the ``id`` / ``name`` / ``aria-label`` attributes, the role (the
    ``role`` attribute, else the accessibility role) and the sha256 of
    :func:`subtree_text`. Two observations of one page give one element the
    same signature whatever Chrome numbered it.
    """
    attributes = node.attributes or {}
    role = attributes.get("role") or node.ax_role or None
    digest = hashlib.sha256(subtree_text(node).encode("utf-8")).hexdigest()
    return (
        node.tag_name.lower(),
        attributes.get("id"),
        attributes.get("name"),
        attributes.get("aria-label"),
        role,
        digest,
    )


def bounds_within(recorded: DOMRect, restored: DOMRect, tolerance_px: float) -> bool:
    """Whether all four bounds values (x, y, width, height) differ by at most *tolerance_px*."""
    return (
        abs(recorded.x - restored.x) <= tolerance_px
        and abs(recorded.y - restored.y) <= tolerance_px
        and abs(recorded.width - restored.width) <= tolerance_px
        and abs(recorded.height - restored.height) <= tolerance_px
    )


# ── retarget ──────────────────────────────────────────────────────────


def _identity(action: Action) -> Retarget:
    return Retarget(action=action.model_copy(deep=True), method="identity")


def _rewritten(action: Action, **params: Any) -> Action:
    copy = action.model_copy(deep=True)
    copy.params.update(params)
    return copy


def retarget_action(
    action: Action, recorded: Any, restored: Any, *, tolerance_px: float
) -> Retarget:
    """How the recorded *action* runs against the *restored* observation (module docstring).

    Computes only: nothing is executed and nothing is kept.
    """
    if action.name == SWITCH_TAB:
        return _retarget_tab(action, recorded, restored)
    is_index_action = action.name in INDEX_ACTIONS or action.name in OPTIONAL_INDEX_ACTIONS
    if not is_index_action or action.params.get("index") is None:
        return _identity(action)
    return _retarget_element(action, recorded, restored, tolerance_px)


def _retarget_element(
    action: Action, recorded: Any, restored: Any, tolerance_px: float
) -> Retarget:
    index = action.params["index"]

    def not_found(detail: str) -> Retarget:
        return Retarget(
            action=None, method="geometry_signature", failure="not_found", detail=detail
        )

    recorded_dom, restored_dom = dom_fragment(recorded), dom_fragment(restored)
    if recorded_dom is None or restored_dom is None:
        side = "recorded" if recorded_dom is None else "restored"
        return not_found(f"the {side} observation has no browser.dom to find index {index!r} in")
    entry = recorded_dom.selector_map.get(index)
    if not isinstance(entry, DOMElementNode):
        return not_found(
            f"index {index!r} is not in the recorded selector map "
            "(the recorded action could not have resolved it)"
        )
    if entry.bounds is None:
        return not_found(f"recorded element {index!r} <{entry.tag_name}> has no bounds")

    signature = element_signature(entry)
    near = [
        (handle, node)
        for handle, node in restored_dom.selector_map.items()
        if isinstance(node, DOMElementNode)
        and node.bounds is not None
        and bounds_within(entry.bounds, node.bounds, tolerance_px)
    ]
    matches = [handle for handle, node in near if element_signature(node) == signature]
    if len(matches) == 1:
        return Retarget(
            action=_rewritten(action, index=matches[0]),
            method="geometry_signature",
            detail=f"index {index} -> {matches[0]} <{entry.tag_name}>",
        )
    if not matches:
        return not_found(
            f"no element within {tolerance_px}px of recorded index {index} <{entry.tag_name}> "
            f"has its signature ({len(near)} candidate(s) by position)"
        )
    return Retarget(
        action=None,
        method="geometry_signature",
        failure="ambiguous",
        detail=f"{len(matches)} candidates match recorded index {index} <{entry.tag_name}>",
    )


def _retarget_tab(action: Action, recorded: Any, restored: Any) -> Retarget:
    target_id = action.params.get("target_id")
    if target_id is None:
        # "The most recently opened tab": no id to translate.
        return _identity(action)

    def not_found(detail: str) -> Retarget:
        return Retarget(action=None, method="tab_order_url", failure="not_found", detail=detail)

    recorded_meta, restored_meta = meta_fragment(recorded), meta_fragment(restored)
    if recorded_meta is None or restored_meta is None:
        side = "recorded" if recorded_meta is None else "restored"
        return not_found(f"the {side} observation has no browser.browser_meta to find tabs in")
    position = next(
        (i for i, tab in enumerate(recorded_meta.tabs) if tab.get("tab_id") == target_id), None
    )
    if position is None:
        return not_found(f"tab {target_id!r} is not among the recorded tabs")
    url = recorded_meta.tabs[position].get("url")
    if position >= len(restored_meta.tabs):
        return not_found(
            f"recorded tab {position} ({url}) is missing: "
            f"{len(restored_meta.tabs)} tab(s) restored, {len(recorded_meta.tabs)} recorded"
        )
    tab = restored_meta.tabs[position]
    if tab.get("url") != url or not tab.get("tab_id"):
        return not_found(f"restored tab {position} is at {tab.get('url')!r}, recorded at {url!r}")
    return Retarget(
        action=_rewritten(action, target_id=tab["tab_id"]),
        method="tab_order_url",
        detail=f"tab {position} ({url})",
    )


# ── measure ───────────────────────────────────────────────────────────


def dhash(image_bytes: bytes) -> int | None:
    """The 64-bit difference hash of an image, or ``None`` when the bytes do not decode.

    Grayscale, resized to 9x8 (Lanczos, so the hash does not depend on a
    library default), and one bit per horizontally adjacent pixel pair:
    set when the left one is brighter.
    """
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            small = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        logger.warning("screenshot does not decode for the perceptual hash: %s", exc)
        return None
    pixels = small.tobytes()  # mode "L": one byte per pixel, row by row
    bits = 0
    for row in range(8):
        for col in range(8):
            left, right = pixels[row * 9 + col], pixels[row * 9 + col + 1]
            bits = (bits << 1) | int(left > right)
    return bits


def hamming(a: int, b: int) -> int:
    """How many bits differ between two hashes."""
    return (a ^ b).bit_count()


def screenshot_distance(recorded: Any, restored: Any) -> int | None:
    """Hamming distance of the two screenshots' :func:`dhash`, or ``None`` without both images."""
    shots = (screenshot_fragment(recorded), screenshot_fragment(restored))
    hashes = [dhash(shot.image_bytes) if shot and shot.image_bytes else None for shot in shots]
    if hashes[0] is None or hashes[1] is None:
        return None
    return hamming(hashes[0], hashes[1])


def signature_multiset(dom: DOMObs) -> Counter[ElementSignature]:
    """The :func:`element_signature` of every indexed element, counted."""
    return Counter(
        element_signature(node)
        for node in dom.selector_map.values()
        if isinstance(node, DOMElementNode)
    )


def observation_url(observation: Any) -> str | None:
    """The captured image's document, or the same cycle's metadata when no image URL exists."""
    screenshot = screenshot_fragment(observation)
    if screenshot is not None and screenshot.source_url:
        return screenshot.source_url
    meta = meta_fragment(observation)
    return meta.url if meta is not None and meta.url else None


def media_measure(
    recorded: Any,
    restored: Any,
    *,
    tolerance_px: float,
    alignment: list[MediaAlignment] | None,
    alignment_detail: str = "",
) -> MediaEvidence | None:
    """The two screenshots' media states compared, with the alignment done before the restored one.

    ``None`` when neither observation has a screenshot and nothing was
    aligned: there is no media evidence to give. A screenshot without media
    state (evidence from before the field) makes ``comparisons`` ``None``,
    not a match.
    """
    shots = (screenshot_fragment(recorded), screenshot_fragment(restored))
    if shots[0] is None and shots[1] is None and alignment is None:
        return None
    return media_evidence(
        shots[0].media if shots[0] is not None else None,
        shots[1].media if shots[1] is not None else None,
        tolerance_px=tolerance_px,
        alignment=alignment,
        alignment_detail=alignment_detail,
    )


def measure(
    recorded: Any,
    restored: Any,
    *,
    capability_id: str,
    document_misses: int | None,
    media: MediaEvidence | None,
) -> RestorationEvidence:
    """What the browser measures between a recorded and a restored observation.

    ``document_misses`` is the capability's own count of main-frame document
    misses since its last measurement (``None`` when the network is live);
    ``media`` is :func:`media_measure`'s comparison with the alignment the
    capability did since its last measurement; the rest is read from the
    two observations.
    """
    recorded_url, restored_url = observation_url(recorded), observation_url(restored)
    url_match: bool | None = None
    if recorded_url is not None and restored_url is not None:
        url_match = recorded_url == restored_url

    signature_match: bool | None = None
    missing = extra = 0
    recorded_dom, restored_dom = dom_fragment(recorded), dom_fragment(restored)
    if recorded_dom is not None and restored_dom is not None:
        before, after = signature_multiset(recorded_dom), signature_multiset(restored_dom)
        missing, extra = (before - after).total(), (after - before).total()
        signature_match = missing == 0 and extra == 0

    return RestorationEvidence(
        capability_id=capability_id,
        url_match=url_match,
        recorded_url=recorded_url,
        restored_url=restored_url,
        signature_match=signature_match,
        signature_missing=missing,
        signature_extra=extra,
        screenshot_distance=screenshot_distance(recorded, restored),
        document_misses=document_misses,
        media=media,
    )


__all__ = [
    "INDEX_ACTIONS",
    "OPTIONAL_INDEX_ACTIONS",
    "SWITCH_TAB",
    "TEXT_DEPTH_LIMIT",
    "ElementSignature",
    "bounds_within",
    "dhash",
    "dom_fragment",
    "element_signature",
    "hamming",
    "measure",
    "media_measure",
    "meta_fragment",
    "observation_url",
    "retarget_action",
    "screenshot_distance",
    "screenshot_fragment",
    "signature_multiset",
    "subtree_text",
]
