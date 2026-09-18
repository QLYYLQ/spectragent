"""Skyvern DOM cleaning strategy — JS-side extraction + server-side trim.

Pipeline:
1. Inject domUtils.js via CDP Runtime.evaluate — runs isInteractable() during
   DOM traversal, collects element tree with ~30 reserved attributes.
2. Receive JSON element tree from JS.
3. Server-side trim: attribute allowlist, purgeable removal, unique IDs.
4. Convert to DOMElementNode tree + build DOMSelectorMap.
5. XPath generation for re-location.

Unlike browser-use's CDP 3-protocol merge approach, skyvern does heavy
lifting in the browser via JavaScript and returns a pre-filtered JSON tree.
"""

from __future__ import annotations

import hashlib
import logging
from importlib import resources
from typing import Any

from spectragent.core.capabilities.browser.dom.cleaning import DOMCleanerBase
from spectragent.core.capabilities.browser.dom.models import (
    DOMElementNode,
    DOMRect,
    DOMSelectorMap,
    DOMState,
    ScrollInfo,
    TargetCapture,
    element_handle,
)

logger = logging.getLogger(__name__)

# Skyvern attribute allowlist — only these survive server-side trimming.
_ALLOWED_ATTRS: frozenset[str] = frozenset(
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
        "checked",
        "data-original-title",
        "data-ui",
        "disabled",
        "for",
        "href",
        "maxlength",
        "name",
        "pattern",
        "placeholder",
        "readonly",
        "required",
        "selected",
        "shape-description",
        "src",
        "title",
        "type",
        "value",
    }
)


class SkyvernCleaner(DOMCleanerBase):
    """Skyvern-style DOM cleaning: JS extraction + server trim."""

    __registry_key__ = "skyvern"

    def __init__(
        self,
        *,
        max_text_length: int = 5000,
        include_purgeable: bool = False,
    ) -> None:
        self.max_text_length = max_text_length
        self.include_purgeable = include_purgeable
        self._js_script: str | None = None

    @property
    def extraction_script(self) -> str:
        """Return the JS extraction script.

        The script is loaded once and cached.  Override this to use a
        custom extraction script.
        """
        if self._js_script is None:
            self._js_script = _load_extraction_script()
        return self._js_script

    async def clean(
        self,
        capture: TargetCapture,
        viewport: tuple[int, int],
        *,
        previous_selector_map: DOMSelectorMap | None = None,
        device_pixel_ratio: float = 1.0,
    ) -> DOMState:
        """Clean using pre-extracted JS result.

        Note: In the actual pipeline, the JS extraction happens via CDP
        Runtime.evaluate *before* this method is called.  The result is
        passed in ``raw_dom["js_elements"]`` or ``raw_dom["elements"]``.
        If not present, falls back to building from snapshot (like browser-use).

        The JS extraction runs in the tab's own document, so every element it
        reports belongs to the tab's target and is keyed by its backend node
        id; out-of-process frames in ``capture.frames`` are not read.
        """
        # Check for JS-extracted elements.
        js_elements = capture.raw_dom.get("js_elements") or capture.raw_dom.get("elements")
        if js_elements:
            # JS path: getBoundingClientRect on the page side already returns
            # CSS pixels, so DPR conversion is not needed here. The kwarg is
            # accepted for protocol parity and future-proofing only.
            return self._from_js_elements(js_elements, viewport, capture.session_id)

        # Fallback: build from DOMSnapshot (less accurate for skyvern).
        logger.warning("SkyvernCleaner: No JS elements found, falling back to snapshot")
        return await self._from_snapshot(capture, viewport, dpr=device_pixel_ratio)

    def _from_js_elements(
        self,
        elements: list[dict[str, Any]],
        viewport: tuple[int, int],
        session_id: str | None,
    ) -> DOMState:
        """Convert JS-extracted element tree to DOMState."""
        root = DOMElementNode(tag_name="body", is_visible=True)
        selector_map: DOMSelectorMap = {}

        for elem_data in elements:
            node = self._convert_element(elem_data, session_id, selector_map)
            if node is not None:
                root.children.append(node)
                node.parent = root

        return DOMState(
            dom_tree=root,
            selector_map=selector_map,
            scroll_info=ScrollInfo(viewport_width=viewport[0], viewport_height=viewport[1]),
        )

    def _convert_element(
        self,
        data: dict[str, Any],
        session_id: str | None,
        selector_map: DOMSelectorMap,
    ) -> DOMElementNode | None:
        """Recursively convert a JS element dict to DOMElementNode."""
        tag = data.get("tagName", "div")
        is_interactable = data.get("interactable", False)
        is_purgeable = data.get("purgeable", False)

        # Skip purgeable elements unless configured to include them.
        if is_purgeable and not self.include_purgeable and not is_interactable:
            text = data.get("text", "")
            if not text or not text.strip():
                return None

        # Filter attributes.
        raw_attrs = data.get("attributes", {})
        attrs = {k: v for k, v in raw_attrs.items() if k in _ALLOWED_ATTRS}

        # Preserve element ID from JS extraction.
        elem_id = data.get("id", "")
        if elem_id:
            attrs["data-skyvern-id"] = elem_id

        # Text content.
        text = data.get("text", "")
        if len(text) > self.max_text_length:
            text = text[: self.max_text_length] + "..."

        # Bounds.
        bounds: DOMRect | None = None
        rect = data.get("rect")
        if rect and isinstance(rect, dict):
            bounds = DOMRect(
                x=rect.get("x", 0),
                y=rect.get("y", 0),
                width=rect.get("width", 0),
                height=rect.get("height", 0),
            )

        node = DOMElementNode(
            tag_name=tag,
            attributes=attrs,
            text_content=text,
            backend_node_id=data.get("backendNodeId"),
            session_id=session_id,
            bounds=bounds,
            is_visible=not is_purgeable,
            is_interactive=is_interactable,
            ax_role=data.get("role"),
            ax_name=data.get("name"),
        )

        # Index by the tab target's key — the backend node id itself (see
        # ``element_handle``) — so the index a serializer prints is the one
        # the selector map answers to.
        handle = (
            element_handle(0, node.backend_node_id)
            if is_interactable and node.backend_node_id is not None
            else None
        )
        if handle is not None:
            node.element_index = handle
            selector_map[handle] = node

        # XPath for re-location.
        xpath = data.get("xpath")
        if xpath:
            node.attributes["data-xpath"] = xpath

        # Recurse children.
        for child_data in data.get("children", []):
            child = self._convert_element(child_data, session_id, selector_map)
            if child is not None:
                node.children.append(child)
                child.parent = node

        return node

    async def _from_snapshot(
        self,
        capture: TargetCapture,
        viewport: tuple[int, int],
        *,
        dpr: float = 1.0,
    ) -> DOMState:
        """Fallback: build from DOMSnapshot (reuse browser-use logic)."""
        from spectragent.core.capabilities.browser.dom.cleaning.browser_use import BrowserUseCleaner

        fallback = BrowserUseCleaner()
        return await fallback.clean(
            TargetCapture(
                raw_dom={},
                snapshot=capture.snapshot,
                ax_tree=capture.ax_tree,
                session_id=capture.session_id,
            ),
            viewport,
            device_pixel_ratio=dpr,
        )


def _load_extraction_script() -> str:
    """Load the domUtils.js extraction script.

    Looks in ``spectragent/core/browser/dom/cleaning/js/domUtils.js``.
    If not found, returns a minimal placeholder.
    """
    try:
        js_path = (
            resources.files("spectragent.core.capabilities.browser.dom.cleaning")
            / "js"
            / "domUtils.js"
        )
        return js_path.read_text(encoding="utf-8")
    except (FileNotFoundError, TypeError):
        logger.warning("domUtils.js not found, skyvern JS extraction will not work")
        return ""


def element_hash(attrs: dict[str, str], tag: str) -> str:
    """SHA-256 hash of element structure (excluding transient attrs)."""
    stable_attrs = {k: v for k, v in sorted(attrs.items()) if k not in ("id", "class", "style")}
    content = f"{tag}:{stable_attrs}"
    return hashlib.sha256(content.encode()).hexdigest()[:16]
