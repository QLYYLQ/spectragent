"""``skyvern_hit_test``: the Skyvern cleaner plus an in-page hit test (M32, arch §4.12).

The ``skyvern`` cleaner indexes every interactive element its page script
finds — including a link a sticky header has covered (the M18 defect): its
JS path records no paint order, and the raw DOM cannot add one. This
variant, separately selectable so that ``skyvern`` itself stays
byte-identical, runs the same extraction and then asks the page, per
element, whether a click at its centre — or, failing that, at one of a 5x5
grid of points inside its visible part — would reach it
(``HIT_TEST_FUNCTION``). Each indexed element in the viewport records the
answer as ``DOMElementNode.hit_test``; with ``covered_elements: drop`` (the
default) an element no probe reached loses its index, so the agent never
sees it.

``clean`` is ``SkyvernCleaner.clean`` followed by a post-process; none of the
base cleaner's code changes.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Literal

from spectragent.core.capabilities.browser.dom.cleaning import DOMCaptureSource
from spectragent.core.capabilities.browser.dom.cleaning.skyvern import SkyvernCleaner
from spectragent.core.capabilities.browser.dom.js.skyvern_capture import capture_skyvern
from spectragent.core.capabilities.browser.dom.models import (
    DOMSelectorMap,
    DOMState,
    HitTest,
    TargetCapture,
    element_handle,
)


class SkyvernHitTestCleaner(SkyvernCleaner):
    """Skyvern's extraction and trimming, plus a recorded hit test per indexed element."""

    __registry_key__ = "skyvern_hit_test"

    def __init__(
        self,
        *,
        max_text_length: int = 5000,
        include_purgeable: bool = False,
        covered_elements: Literal["drop", "keep"] = "drop",
    ) -> None:
        super().__init__(max_text_length=max_text_length, include_purgeable=include_purgeable)
        self.covered_elements = covered_elements

    async def capture(self, source: DOMCaptureSource) -> tuple[TargetCapture, float]:
        """The Skyvern extraction with the hit test on the same remote object."""
        return await capture_skyvern(source, hit_test=True), 1.0  # JS rects are CSS px

    async def clean(
        self,
        capture: TargetCapture,
        viewport: tuple[int, int],
        *,
        previous_selector_map: DOMSelectorMap | None = None,
        device_pixel_ratio: float = 1.0,
    ) -> DOMState:
        """``SkyvernCleaner.clean``, then each hit test onto the element it tested.

        A result maps through its ref to the element's backend node id, then
        to its selector-map key in its target's namespace — the key
        ``SkyvernCleaner`` gave it. ``visible_point`` is the page script's
        offset added to the element's top-level page box, minus the top-level
        document's scroll: CSS px of the top-level viewport, iframe offsets
        included. An element the page found outside its frame's viewport has
        no result and keeps ``hit_test=None`` and its index — off-screen is
        not covered.
        """
        state = await super().clean(
            capture,
            viewport,
            previous_selector_map=previous_selector_map,
            device_pixel_ratio=device_pixel_ratio,
        )
        scroll = (state.scroll_info.scroll_x, state.scroll_info.scroll_y)
        covered: list[int] = []
        for handle, result in _hit_tests(capture):
            node = state.selector_map.get(handle)
            if node is None or node.bounds is None or not isinstance(result, dict):
                continue
            offset = result.get("offset")
            point = (
                (
                    node.bounds.x + float(offset[0]) - scroll[0],
                    node.bounds.y + float(offset[1]) - scroll[1],
                )
                if isinstance(offset, list) and len(offset) == 2
                else None
            )
            node.hit_test = HitTest(
                center_topmost=bool(result.get("center_topmost")), visible_point=point
            )
            if not node.hit_test.center_topmost and point is None:
                covered.append(handle)
        if self.covered_elements == "drop":
            for handle in covered:
                state.selector_map.pop(handle).element_index = None
        return state


def _hit_tests(capture: TargetCapture) -> Iterator[tuple[int, Any]]:
    """``(selector-map key, result)`` for every hit-test result of *capture* and its frames."""
    nodes = capture.raw_dom.get("skyvern_nodes") or {}
    for ref, result in (capture.raw_dom.get("skyvern_hit_tests") or {}).items():
        backend_id = (nodes.get(ref) or {}).get("backendNodeId")
        if isinstance(backend_id, int):
            handle = element_handle(capture.namespace, backend_id)
            if handle is not None:
                yield handle, result
    for child in capture.frames.values():
        yield from _hit_tests(child)


__all__ = ["SkyvernHitTestCleaner"]
