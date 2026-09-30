"""Skyvern-style page-side extraction and server-side DOM trimming.

The configured instance is a strategy, not a tab session. ``capture`` gets
fresh, tab-bound CDP and JS execution abilities for each observation. The
page-side algorithm is ours (``dom/js/skyvern_extraction.py``); no upstream
``domUtils.js`` is packaged or loaded.
"""

from __future__ import annotations

from typing import Any

from spectragent.core.capabilities.browser.dom.cleaning import DOMCaptureSource, DOMCleanerBase
from spectragent.core.capabilities.browser.dom.js.skyvern_capture import capture_skyvern
from spectragent.core.capabilities.browser.dom.models import (
    DOMElementNode,
    DOMRect,
    DOMSelectorMap,
    DOMState,
    ScrollInfo,
    TargetCapture,
    element_handle,
)

_ALLOWED_ATTRS = frozenset(
    {
        "accept",
        "alt",
        "aria-checked",
        "aria-current",
        "aria-disabled",
        "aria-label",
        "aria-readonly",
        "aria-required",
        "aria-role",
        "aria-selected",
        "autocomplete",
        "checked",
        "class",
        "data-af-redacted",
        "data-testid",
        "disabled",
        "for",
        "href",
        "id",
        "maxlength",
        "name",
        "pattern",
        "placeholder",
        "readonly",
        "required",
        "role",
        "selected",
        "src",
        "title",
        "type",
        "value",
    }
)


def _rect(value: Any) -> DOMRect | None:
    if not isinstance(value, dict):
        return None
    return DOMRect(
        x=float(value.get("x") or 0),
        y=float(value.get("y") or 0),
        width=float(value.get("width") or 0),
        height=float(value.get("height") or 0),
    )


class SkyvernCleaner(DOMCleanerBase):
    """A JS traversal with CDP-backed element identity and a trimmed tree."""

    __registry_key__ = "skyvern"

    def __init__(
        self,
        *,
        max_text_length: int = 5000,
        include_purgeable: bool = False,
    ) -> None:
        self.max_text_length = max_text_length
        self.include_purgeable = include_purgeable

    async def capture(self, source: DOMCaptureSource) -> tuple[TargetCapture, float]:
        """Ask the JS scope to extract instead of running the CDP triple."""
        return await capture_skyvern(source), 1.0  # JS rects are already CSS pixels.

    async def clean(
        self,
        capture: TargetCapture,
        viewport: tuple[int, int],
        *,
        previous_selector_map: DOMSelectorMap | None = None,
        device_pixel_ratio: float = 1.0,
    ) -> DOMState:
        """Turn one complete JS capture into the normal DOMState contract.

        Missing JS data is an error. A snapshot fallback would advertise a
        selected Skyvern strategy while silently running a worse BrowserUse
        strategy, and would lose same-process frame content.
        """
        if not isinstance(capture.raw_dom.get("skyvern_tree"), dict):
            raise RuntimeError("Skyvern DOM capture contains no JS element tree")
        selector_map: DOMSelectorMap = {}
        scroll = capture.raw_dom.get("skyvern_scroll") or {}
        root = self._build_target(
            capture,
            selector_map=selector_map,
            previous_selector_map=previous_selector_map,
            origin=(float(scroll.get("x") or 0), float(scroll.get("y") or 0)),
        )
        return DOMState(
            dom_tree=root,
            selector_map=selector_map,
            scroll_info=ScrollInfo(
                scroll_x=float(scroll.get("x") or 0),
                scroll_y=float(scroll.get("y") or 0),
                scroll_width=float(scroll.get("width") or 0),
                scroll_height=float(scroll.get("height") or 0),
                viewport_width=float(scroll.get("viewportWidth") or viewport[0]),
                viewport_height=float(scroll.get("viewportHeight") or viewport[1]),
            ),
        )

    def _build_target(
        self,
        capture: TargetCapture,
        *,
        selector_map: DOMSelectorMap,
        previous_selector_map: DOMSelectorMap | None,
        origin: tuple[float, float],
    ) -> DOMElementNode:
        raw = capture.raw_dom
        tree = raw.get("skyvern_tree")
        if not isinstance(tree, dict):
            raise RuntimeError("Skyvern frame capture contains no JS element tree")
        node = self._build_node(
            tree,
            capture,
            selector_map=selector_map,
            previous_selector_map=previous_selector_map,
            origin=origin,
        )
        if node is None:
            raise RuntimeError("Skyvern JS capture produced no visible document root")
        return node

    def _build_node(
        self,
        data: dict[str, Any],
        capture: TargetCapture,
        *,
        selector_map: DOMSelectorMap,
        previous_selector_map: DOMSelectorMap | None,
        origin: tuple[float, float],
    ) -> DOMElementNode | None:
        tag = str(data.get("tagName") or "div").lower()
        text = str(data.get("text") or "")
        if len(text) > self.max_text_length:
            text = text[: self.max_text_length] + "…"
        interactive = bool(data.get("interactive"))
        if (
            not self.include_purgeable
            and tag in {"div", "span"}
            and not interactive
            and not text
            and not data.get("children")
            and not data.get("shadowRoots")
        ):
            return None

        attributes = {
            str(key): str(value)
            for key, value in (data.get("attributes") or {}).items()
            if key in _ALLOWED_ATTRS
        }
        details = (capture.raw_dom.get("skyvern_nodes") or {}).get(data.get("ref"), {})
        backend_id = details.get("backendNodeId")
        if not isinstance(backend_id, int):
            backend_id = None
        bounds = _rect(data.get("rect"))
        if bounds is not None:
            bounds.x += origin[0]
            bounds.y += origin[1]
        client_rect = _rect(data.get("clientRect"))
        scroll_rects = _rect(data.get("scrollRects"))
        node = DOMElementNode(
            tag_name=tag,
            node_type=3 if tag == "#text" else 11 if tag == "#shadow-root" else 1,
            node_value=text if tag == "#text" else "",
            attributes=attributes,
            text_content="" if tag == "#text" else text,
            backend_node_id=backend_id,
            node_id=details.get("nodeId"),
            frame_id=capture.raw_dom.get("skyvern_frame_id"),
            target_id=capture.target_id,
            session_id=capture.session_id,
            bounds=bounds,
            client_rect=client_rect,
            scroll_rects=scroll_rects,
            is_visible=True,
            is_interactive=interactive,
            is_scrollable=bool(
                client_rect
                and scroll_rects
                and (
                    scroll_rects.width > client_rect.width + 1
                    or scroll_rects.height > client_rect.height + 1
                )
            ),
            ax_role=attributes.get("role"),
            ax_name=attributes.get("aria-label"),
            shadow_root_type=data.get("shadowRootType"),
            _handle_namespace=capture.namespace,
        )
        if interactive and backend_id is not None:
            handle = element_handle(capture.namespace, backend_id)
            if handle is not None:
                node.element_index = handle
                node.is_new = (
                    previous_selector_map is not None and handle not in previous_selector_map
                )
                selector_map[handle] = node

        for child_data in data.get("children") or []:
            child = self._build_node(
                child_data,
                capture,
                selector_map=selector_map,
                previous_selector_map=previous_selector_map,
                origin=origin,
            )
            if child is not None:
                child.parent = node
                node.children.append(child)
        for shadow_data in data.get("shadowRoots") or []:
            shadow = self._build_node(
                shadow_data,
                capture,
                selector_map=selector_map,
                previous_selector_map=previous_selector_map,
                origin=origin,
            )
            if shadow is not None:
                shadow.parent = node
                if node.shadow_roots is None:
                    node.shadow_roots = []
                node.shadow_roots.append(shadow)

        if tag in {"iframe", "frame"} and bounds is not None:
            owners = capture.raw_dom.get("skyvern_frame_owners") or {}
            child_frame_id = details.get("frameId")
            if not isinstance(child_frame_id, str):
                child_frame_id = next(
                    (key for key, value in owners.items() if value == backend_id), None
                )
            child_capture = capture.frames.get(child_frame_id) if child_frame_id else None
            if child_capture is not None:
                content_offset = data.get("contentOffset") or {}
                frame_origin = (
                    bounds.x + float(content_offset.get("x") or 0),
                    bounds.y + float(content_offset.get("y") or 0),
                )
                content = self._build_target(
                    child_capture,
                    selector_map=selector_map,
                    previous_selector_map=previous_selector_map,
                    origin=frame_origin,
                )
                content.parent = node
                node.content_document = content
        return node
