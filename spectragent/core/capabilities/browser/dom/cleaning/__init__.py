"""DOM cleaning strategy registry.

Two strategies registered via conscribe:
- ``browser_use``: 5-stage pipeline (simplify → optimize → paint-order → bounds → index)
- ``skyvern``:     JS extraction (Runtime.evaluate domUtils.js) + server-side trim
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from conscribe import create_registrar

from spectragent.core.capabilities.browser.dom.models import (
    DOMSelectorMap,
    DOMState,
    TargetCapture,
)


@runtime_checkable
class DOMCleanerProtocol(Protocol):
    """Strategy for converting raw CDP data into a clean DOMState."""

    async def clean(
        self,
        capture: TargetCapture,
        viewport: tuple[int, int],
        *,
        previous_selector_map: DOMSelectorMap | None = None,
        device_pixel_ratio: float = 1.0,
    ) -> DOMState:
        """Clean and filter raw CDP DOM data into a DOMState.

        Parameters
        ----------
        capture:
            The tab's own target — ``DOM.getDocument(depth=-1, pierce=True)``,
            ``DOMSnapshot.captureSnapshot(...)``, the accessibility nodes of
            each of its frames, optional JS click-listener ids — with the
            captures of its out-of-process frames nested in
            ``capture.frames``. A cleaner that reads frame content must key
            the selector map with ``dom.models.element_handle``: backend node
            ids of different targets collide.
        viewport:
            ``(width, height)`` in CSS pixels.
        previous_selector_map:
            Previous selector_map for new-node tracking. ``None`` on first call.
        device_pixel_ratio:
            Live ``window.devicePixelRatio`` for the target.  CDP
            ``DOMSnapshot`` returns ``bounds`` in **device pixels**;
            divide by DPR to recover CSS pixels expected by every
            downstream consumer (click coords, occlusion, serialization).
            Default ``1.0`` keeps tests and DPR-1 environments unchanged.
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
