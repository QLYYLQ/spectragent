"""DOM cleaning strategy registry.

Two strategies registered via conscribe:
- ``browser_use``: 5-stage pipeline (simplify → optimize → paint-order → bounds → index)
- ``skyvern``:     JS extraction (Runtime.evaluate domUtils.js) + server-side trim
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from conscribe import create_registrar

from spectragent.core.capabilities.browser.dom.models import DOMSelectorMap, DOMState


@runtime_checkable
class DOMCleanerProtocol(Protocol):
    """Strategy for converting raw CDP data into a clean DOMState."""

    async def clean(
        self,
        raw_dom: dict[str, Any],
        snapshot: dict[str, Any],
        ax_tree: dict[str, Any],
        viewport: tuple[int, int],
        *,
        session_id: str | None = None,
        js_click_listener_ids: set[int] | None = None,
        previous_selector_map: DOMSelectorMap | None = None,
    ) -> DOMState:
        """Clean and filter raw CDP DOM data into a DOMState.

        Parameters
        ----------
        raw_dom:
            Result of ``DOM.getDocument(depth=-1, pierce=True)``.
        snapshot:
            Result of ``DOMSnapshot.captureSnapshot(...)``.
        ax_tree:
            Result of ``Accessibility.getFullAXTree()``.
        viewport:
            ``(width, height)`` in CSS pixels.
        session_id:
            CDP session ID (for multi-target filtering).
        js_click_listener_ids:
            Backend node IDs of elements with JS click event listeners
            (from optional 4th CDP call). ``None`` means detection was skipped.
        previous_selector_map:
            Previous selector_map for new-node tracking. ``None`` on first call.
        """
        ...


DOMCleanerRegistrar = create_registrar(
    "dom_cleaner",
    DOMCleanerProtocol,
    discriminator_field="cleaner_type",
)


class DOMCleanerBase(metaclass=DOMCleanerRegistrar.Meta):
    """Base for DOM cleaning strategies — subclass to auto-register."""

    __abstract__ = True
    __registry_key__: str

    @property
    def cleaner_type(self) -> str:
        return self.__registry_key__
